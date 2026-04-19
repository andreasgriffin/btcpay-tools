from __future__ import annotations

import enum
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
import logging

logger = logging.getLogger(__file__)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore


class PlanDuration(str, enum.Enum):
    MONTH = "month"
    YEAR = "year"


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SubscriptionProduct(ConfigModel):
    pos_id: str
    trial_pos_id: str
    offering_id: str
    plan_id: str
    duration: PlanDuration


ProductCatalog = dict[str, list[SubscriptionProduct]]


class BTCPayBaseConfig(ConfigModel):
    base_url: str
    pos_app_id: str
    store_id: str
    api_key: str | None = None

    def subscription_pos_base_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/apps/{self.pos_app_id}/pos"


class ClientConfig(ConfigModel):
    npub_bitcoin_safe_pos: str
    default_product: str | None = None
    subscriber_email: str | None = None
    receipt_data: dict[str, Any] | list[Any] | None = None


class DaemonConfig(ConfigModel):
    poll_seconds: int = 10
    http_timeout_seconds: int = 20
    portal_duration_minutes: int = 1440
    max_delivery_attempts: int = 10
    initial_lookback_seconds: int = 0
    delivery_timeout_seconds: int = 5
    max_pages: int = 5
    state_file: Path = Path("btcpay_tools/btcpay_subscription_nostr/daemon.state.json")
    reuse_existing_subscriber_by_email: bool = True
    nsec_bitcoin_safe_pos: str | None = None


class BTCPayConfig(ConfigModel):
    client: ClientConfig
    btcpay_base: BTCPayBaseConfig
    daemon: DaemonConfig | None = None
    products: ProductCatalog = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_product_pos_ids(self) -> BTCPayConfig:
        self._validate_unique_subscription_duration()
        self._validate_unique_plan_ids()
        self._validate_unique_product_field("pos_id")
        self._validate_unique_product_field("trial_pos_id")
        return self

    def _validate_unique_subscription_duration(self) -> None:
        for product_id, subscriptions in self.products.items():
            seen_durations: set[PlanDuration] = set()
            for subscription in subscriptions:
                if subscription.duration in seen_durations:
                    raise ValueError(
                        "Duplicate products duration value "
                        f"{subscription.duration.value!r} for product {product_id}"
                    )
                seen_durations.add(subscription.duration)

    def _validate_unique_product_field(self, field_name: str) -> None:
        seen_values: dict[str, str] = {}
        for subscription_id, product in self.iter_subscription_products():
            value = {
                "pos_id": product.pos_id,
                "trial_pos_id": product.trial_pos_id,
            }[field_name]
            duplicate = seen_values.get(value)
            if duplicate is not None:
                raise ValueError(
                    f"Duplicate products.{field_name} value "
                    f"{value!r} for products {duplicate} and {subscription_id}"
                )
            seen_values[value] = subscription_id

    def _validate_unique_plan_ids(self) -> None:
        seen_plan_ids: set[str] = set()
        for _, subscription in self.iter_subscription_products():
            if subscription.plan_id in seen_plan_ids:
                raise ValueError(
                    f"Duplicate products.plan_id value {subscription.plan_id!r}"
                )
            seen_plan_ids.add(subscription.plan_id)

    def iter_subscription_products(self) -> list[tuple[str, SubscriptionProduct]]:
        return [
            (subscription.plan_id, subscription)
            for subscriptions in self.products.values()
            for subscription in subscriptions
        ]

    def subscription_products(self) -> dict[str, SubscriptionProduct]:
        return {
            subscription_id: subscription
            for subscription_id, subscription in self.iter_subscription_products()
        }

    def plans(self, product_id: str) -> tuple[SubscriptionProduct, ...]:
        subscriptions = self.products.get(product_id)
        if subscriptions is None:
            raise KeyError(product_id)
        return tuple(subscriptions)

    def resolve_subscription(
        self,
        product_id: str,
        duration: PlanDuration | str | None = None,
    ) -> SubscriptionProduct:
        subscriptions = self.products.get(product_id)
        if subscriptions is None:
            raise KeyError(product_id)
        if duration is None:
            if len(subscriptions) == 1:
                return subscriptions[0]
            raise ValueError(
                f"Product {product_id!r} has multiple subscriptions; pass a duration"
            )
        resolved_duration = (
            duration if isinstance(duration, PlanDuration) else PlanDuration(duration)
        )
        for subscription in subscriptions:
            if subscription.duration == resolved_duration:
                return subscription
        raise ValueError(
            f"Product {product_id!r} has no {resolved_duration.value!r} subscription"
        )

    def subscription_pos_base_url(self) -> str:
        return self.btcpay_base.subscription_pos_base_url()

    @property
    def npub_bitcoin_safe_pos(self) -> str:
        return self.client.npub_bitcoin_safe_pos

    @classmethod
    def default_local_path(cls) -> Path:
        return Path("btcpay_subscription_nostr.local.yaml")

    @classmethod
    def load_plugin_btcpay_config(
        cls, module_file: Path | str, package_name: str
    ) -> BTCPayConfig:
        return _load_plugin_btcpay_config(
            cls, module_file=module_file, package_name=package_name
        )

    @classmethod
    def load(cls, source: Path | str) -> BTCPayConfig:
        if isinstance(source, Path):
            return cls.load_file(source)
        candidate_path = Path(source)
        if "\n" not in source and candidate_path.exists():
            return cls.load_file(candidate_path)
        return cls.loads(source)

    @classmethod
    def load_file(cls, path: Path | str) -> BTCPayConfig:
        resolved_path = Path(path)
        if not resolved_path.exists():
            raise RuntimeError(f"Config file {resolved_path} does not exist")
        return cls.loads(
            resolved_path.read_text(encoding="utf-8"), source=resolved_path
        )

    @classmethod
    def loads(cls, yaml_string: str, source: Path | None = None) -> BTCPayConfig:
        raw = yaml.safe_load(yaml_string)
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            if source is None:
                raise RuntimeError("Config YAML must contain a YAML mapping")
            raise RuntimeError(f"Config file {source} must contain a YAML mapping")
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise RuntimeError(str(exc)) from exc

    @property
    def data(self) -> dict[str, Any]:
        return self.model_dump(mode="python", by_alias=True)


def _load_btcpay_config_from_mapping(
    config_cls: type[BTCPayConfig], data: Any, source_path: Path
) -> BTCPayConfig:
    if not isinstance(data, dict):
        raise RuntimeError(f"{source_path} must contain a mapping")

    btcpay_data = data.get("btcpay")
    if not isinstance(btcpay_data, dict):
        raise RuntimeError(f"{source_path} must define a 'btcpay' mapping")

    try:
        return config_cls.model_validate(btcpay_data)
    except ValidationError as exc:
        raise RuntimeError(f"{source_path} has invalid 'btcpay' config: {exc}") from exc


@cache
def _load_plugin_btcpay_config(
    config_cls: type[BTCPayConfig], module_file: Path | str, package_name: str
) -> BTCPayConfig:
    module_path = Path(module_file).resolve()
    package_dir = module_path.parent
    plugin_manifest_path = package_dir / "plugin.yaml"
    if plugin_manifest_path.is_file():
        manifest_data = yaml.safe_load(plugin_manifest_path.read_text(encoding="utf-8"))
        return _load_btcpay_config_from_mapping(
            config_cls, manifest_data, plugin_manifest_path
        )

    pyproject_path = package_dir.parent / "pyproject.toml"
    if pyproject_path.is_file():
        pyproject_data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        plugin_data = (
            pyproject_data.get("tool", {}).get("bitcoin_safe", {}).get("plugin")
            if isinstance(pyproject_data, dict)
            else None
        )
        return _load_btcpay_config_from_mapping(config_cls, plugin_data, pyproject_path)

    raise RuntimeError(
        f"Could not locate BTCPay config for {package_name}. "
        f"Expected {plugin_manifest_path} or {pyproject_path}."
    )
