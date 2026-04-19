from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from btcpay_tools.config import BTCPayBaseConfig, BTCPayConfig, PlanDuration
from btcpay_tools.btcpay_subscription_nostr import client as client_module
from btcpay_tools.btcpay_subscription_nostr.client import build_client


def write_config(tmp_path: Path, body: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(body.strip(), encoding="utf-8")
    return config_path


def test_load_product_catalog_requires_offering_plan_and_pos_ids(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: demo-paid-pos
      trial_pos_id: demo-pos
      duration: month
client:
  npub_bitcoin_safe_pos: npub1example
""",
    )

    products = BTCPayConfig.load_file(config_path).products

    product = products["demo"][0]
    assert product.offering_id == "offering123"
    assert product.plan_id == "plan123"
    assert product.pos_id == "demo-paid-pos"
    assert product.trial_pos_id == "demo-pos"
    assert product.duration == PlanDuration.MONTH


def test_config_load_accepts_yaml_string() -> None:
    config = BTCPayConfig.load(
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: demo-paid-pos
      trial_pos_id: demo-trial-pos
      duration: year
client:
  npub_bitcoin_safe_pos: npub1example
"""
    )

    assert config.products["demo"][0].pos_id == "demo-paid-pos"
    assert config.products["demo"][0].trial_pos_id == "demo-trial-pos"
    assert config.products["demo"][0].duration == PlanDuration.YEAR


def test_config_load_accepts_path_string(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
client:
  npub_bitcoin_safe_pos: npub1example
    """,
    )

    config = BTCPayConfig.load(str(config_path))

    assert config.btcpay_base is not None
    assert config.btcpay_base.store_id == "store123"


def test_config_load_file_rejects_missing_file_with_runtime_error(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.yaml"

    with pytest.raises(RuntimeError, match="does not exist"):
        BTCPayConfig.load_file(missing_path)


def test_config_load_keeps_secret_keys_optional() -> None:
    config = BTCPayConfig.load(
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: demo-paid-pos
      trial_pos_id: demo-trial-pos
      duration: month
client:
  npub_bitcoin_safe_pos: npub1example
"""
    )

    assert config.btcpay_base is not None
    assert config.btcpay_base.api_key is None
    assert config.client.npub_bitcoin_safe_pos == "npub1example"
    assert config.daemon is None


def test_btcpay_base_config_builds_subscription_pos_base_url() -> None:
    config = BTCPayBaseConfig(
        base_url="https://example.com/",
        pos_app_id="pos123",
        store_id="store123",
    )

    assert config.subscription_pos_base_url() == "https://example.com/apps/pos123/pos"


@pytest.mark.parametrize(
    "missing_field",
    [
        "offering_id",
        "plan_id",
        "pos_id",
        "trial_pos_id",
        "duration",
    ],
)
def test_load_product_catalog_rejects_missing_required_fields(
    tmp_path: Path,
    missing_field: str,
) -> None:
    product_fields = {
        "offering_id": "offering123",
        "plan_id": "plan123",
        "pos_id": "demo-paid-pos",
        "trial_pos_id": "demo-trial-pos",
        "duration": "month",
    }
    product_fields.pop(missing_field)
    config_path = write_config(
        tmp_path,
        "\n".join(
            [
                "btcpay_base:",
                "  base_url: https://example.com",
                "  pos_app_id: pos123",
                "  store_id: store123",
                "products:",
                "  demo:",
                *[
                    f"    - {key}: {value}" if index == 0 else f"      {key}: {value}"
                    for index, (key, value) in enumerate(product_fields.items())
                ],
                "client:",
                "  npub_bitcoin_safe_pos: npub1example",
            ]
        ),
    )

    with pytest.raises(RuntimeError, match=missing_field):
        BTCPayConfig.load_file(config_path).products


def test_load_product_catalog_rejects_duplicate_pos_ids(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: shared-pos
      trial_pos_id: demo-trial-pos
      duration: month
  business:
    - offering_id: offering456
      plan_id: plan456
      pos_id: shared-pos
      trial_pos_id: business-trial-pos
      duration: year
client:
  npub_bitcoin_safe_pos: npub1example
""",
    )

    with pytest.raises(RuntimeError, match="Duplicate products.pos_id value"):
        BTCPayConfig.load_file(config_path).products


def test_load_product_catalog_rejects_duplicate_trial_pos_ids(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: demo-paid-pos
      trial_pos_id: shared-trial-pos
      duration: month
  business:
    - offering_id: offering456
      plan_id: plan456
      pos_id: business-paid-pos
      trial_pos_id: shared-trial-pos
      duration: year
client:
  npub_bitcoin_safe_pos: npub1example
""",
    )

    with pytest.raises(RuntimeError, match="Duplicate products.trial_pos_id value"):
        BTCPayConfig.load_file(config_path).products


def test_build_client_uses_selected_product_duration(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: demo-paid-pos
      trial_pos_id: demo-trial-pos
      duration: month
    - offering_id: offering456
      plan_id: plan456
      pos_id: demo-yearly-pos
      trial_pos_id: demo-yearly-trial-pos
      duration: year
client:
  subscriber_email: user@example.com
  npub_bitcoin_safe_pos: npub1example
""",
    )
    args = argparse.Namespace(
        config=config_path,
        product="demo",
        pos_base_url="https://example.com/apps/pos123/pos",
        base_url="https://example.com",
        pos_app_id="pos123",
        subscriber_email="user@example.com",
        to_be_signed="",
        duration="year",
    )
    product_catalog = BTCPayConfig.load_file(config_path).products

    with patch(
        "btcpay_tools.btcpay_subscription_nostr.client.SubscriptionPurchaseClient"
    ) as mocked_client:
        build_client(args, product_catalog)

    assert mocked_client.call_args.args[1] == "demo-yearly-trial-pos"


def test_subscription_products_flattens_grouped_catalog() -> None:
    config = BTCPayConfig.load(
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: demo-paid-pos
      trial_pos_id: demo-trial-pos
      duration: month
    - offering_id: offering456
      plan_id: plan456
      pos_id: demo-yearly-pos
      trial_pos_id: demo-yearly-trial-pos
      duration: year
client:
  npub_bitcoin_safe_pos: npub1example
"""
    )

    assert set(config.subscription_products()) == {"plan123", "plan456"}


def test_btcpay_config_exposes_product_plans_and_client_helpers() -> None:
    config = BTCPayConfig.load(
        """
btcpay_base:
  base_url: https://example.com/
  pos_app_id: pos123
  store_id: store123
products:
  demo:
    - offering_id: offering123
      plan_id: plan123
      pos_id: demo-paid-pos
      trial_pos_id: demo-trial-pos
      duration: month
client:
  npub_bitcoin_safe_pos: npub1example
"""
    )

    assert config.plans("demo")[0].plan_id == "plan123"
    assert config.subscription_pos_base_url() == "https://example.com/apps/pos123/pos"
    assert config.npub_bitcoin_safe_pos == "npub1example"


def test_client_status_parse_args_does_not_require_products(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
btcpay_base:
  base_url: https://example.com
  pos_app_id: pos123
  store_id: store123
client:
  npub_bitcoin_safe_pos: npub1example
""",
    )

    with patch.object(
        sys,
        "argv",
        [
            "client",
            "--config",
            str(config_path),
            "status",
            "--management-url",
            "https://example.com/manage",
        ],
    ):
        args = client_module.parse_args()

    assert args.command == "status"
    assert args.management_url == "https://example.com/manage"
