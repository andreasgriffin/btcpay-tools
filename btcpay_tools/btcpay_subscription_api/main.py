from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
import yaml

from btcpay_tools.btcpay_subscription_api.btcpay_client import (
    BtcpayClient,
    BtcpayConfig,
    customer_selector_from_checkout,
)
from btcpay_tools.btcpay_subscription_api.models import (
    PlanCatalogItem,
    PlanCatalogResponse,
    StoreInfoResponse,
    SubscriptionStatusResponse,
)

logger = logging.getLogger("btcpay_subscription_api")
EMAIL_PATTERN = re.compile(
    r"^[^\s@<>]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
MAX_PLAN_ID_LENGTH = 128
MAX_EMAIL_LENGTH = 254
MANAGEMENT_URL_PATH = "/api/subscriptions/management-url"
STATUS_PATH = "/api/subscriptions/status"
CATALOG_PATH = "/api/subscription-plans"
STORE_PATH = "/api/store"


class BtcpayGateway(Protocol):
    def get_subscriber(
        self,
        offering_id: str,
        customer_selector: str,
    ) -> dict[str, Any] | None: ...

    def get_offering_plan(self, offering_id: str, plan_id: str) -> dict[str, Any]: ...

    def create_plan_checkout(
        self,
        offering_id: str,
        plan_id: str,
        subscriber_email: str,
        is_trial: bool,
    ) -> dict[str, Any]: ...

    def proceed_plan_checkout(
        self,
        checkout_id: str,
        subscriber_email: str,
    ) -> dict[str, Any]: ...

    def get_plan_checkout(self, checkout_id: str) -> dict[str, Any]: ...

    def create_portal_session(
        self,
        offering_id: str,
        customer_selector: str,
        duration_minutes: int,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SubscriptionPlan:
    offering_id: str


@dataclass(frozen=True)
class ApiConfig:
    btcpay_base_url: str
    store_id: str
    api_key: str
    plans: dict[str, SubscriptionPlan]
    portal_duration_minutes: int
    bind_host: str
    port: int
    request_timeout_seconds: float
    max_request_bytes: int
    max_concurrent_workflows: int
    rate_limit_requests: int
    rate_limit_window_seconds: int
    rate_limit_max_clients: int
    create_rate_limit_requests: int
    create_rate_limit_window_seconds: int
    global_create_rate_limit_requests: int
    global_create_rate_limit_window_seconds: int
    activation_timeout_seconds: float
    activation_poll_seconds: float
    status_rate_limit_requests: int
    status_rate_limit_window_seconds: int
    catalog_cache_ttl_seconds: int

    @classmethod
    def from_env(cls) -> ApiConfig:
        config_path = Path(
            os.environ.get(
                "BTCPAY_CONFIG_PATH",
                "/app/btcpay_subscription.yaml",
            )
        )
        btcpay_config = _load_btcpay_config(config_path)
        config = cls(
            btcpay_base_url=btcpay_config["base_url"],
            store_id=btcpay_config["store_id"],
            api_key=btcpay_config["api_key"],
            plans=btcpay_config["plans"],
            portal_duration_minutes=_env_int(
                "PORTAL_DURATION_MINUTES",
                btcpay_config["portal_duration_minutes"],
            ),
            bind_host=os.environ.get("BIND_HOST", "0.0.0.0"),
            port=_env_int("PORT", 8080),
            request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS", 5),
            max_request_bytes=_env_int("MAX_REQUEST_BYTES", 4096),
            max_concurrent_workflows=_env_int("MAX_CONCURRENT_WORKFLOWS", 8),
            rate_limit_requests=_env_int("RATE_LIMIT_REQUESTS", 60),
            rate_limit_window_seconds=_env_int("RATE_LIMIT_WINDOW_SECONDS", 60),
            rate_limit_max_clients=_env_int("RATE_LIMIT_MAX_CLIENTS", 10000),
            create_rate_limit_requests=_env_int("CREATE_RATE_LIMIT_REQUESTS", 2),
            create_rate_limit_window_seconds=_env_int(
                "CREATE_RATE_LIMIT_WINDOW_SECONDS", 3600
            ),
            global_create_rate_limit_requests=_env_int(
                "GLOBAL_CREATE_RATE_LIMIT_REQUESTS", 10
            ),
            global_create_rate_limit_window_seconds=_env_int(
                "GLOBAL_CREATE_RATE_LIMIT_WINDOW_SECONDS", 3600
            ),
            activation_timeout_seconds=_env_float("ACTIVATION_TIMEOUT_SECONDS", 20),
            activation_poll_seconds=_env_float("ACTIVATION_POLL_SECONDS", 1),
            status_rate_limit_requests=_env_int("STATUS_RATE_LIMIT_REQUESTS", 30),
            status_rate_limit_window_seconds=_env_int(
                "STATUS_RATE_LIMIT_WINDOW_SECONDS", 60
            ),
            catalog_cache_ttl_seconds=_env_int("CATALOG_CACHE_TTL_SECONDS", 300),
        )
        config.validate()
        return config

    def validate(self) -> None:
        parsed_url = urlsplit(self.btcpay_base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("BTCPAY_BASE_URL must be an HTTP(S) base URL")
        if not self.store_id or not self.api_key:
            raise ValueError("BTCPay store ID and API key must not be empty")
        if not self.plans:
            raise ValueError("BTCPay config must define at least one subscription plan")
        if any(
            not plan_id or len(plan_id) > MAX_PLAN_ID_LENGTH for plan_id in self.plans
        ):
            raise ValueError("BTCPay config contains an invalid subscription plan ID")
        if not 1 <= self.portal_duration_minutes <= 43200:
            raise ValueError("PORTAL_DURATION_MINUTES must be between 1 and 43200")
        if not self.bind_host or not 1 <= self.port <= 65535:
            raise ValueError("BIND_HOST or PORT is invalid")
        if not 1 <= self.request_timeout_seconds <= 30:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be between 1 and 30")
        if not 1 <= self.max_request_bytes <= 65536:
            raise ValueError("MAX_REQUEST_BYTES must be between 1 and 65536")
        if not 1 <= self.max_concurrent_workflows <= 128:
            raise ValueError("MAX_CONCURRENT_WORKFLOWS must be between 1 and 128")
        if not 1 <= self.rate_limit_requests <= 10000:
            raise ValueError("RATE_LIMIT_REQUESTS must be between 1 and 10000")
        if not 1 <= self.rate_limit_window_seconds <= 86400:
            raise ValueError("RATE_LIMIT_WINDOW_SECONDS must be between 1 and 86400")
        if not 1 <= self.rate_limit_max_clients <= 100000:
            raise ValueError("RATE_LIMIT_MAX_CLIENTS must be between 1 and 100000")
        if not 1 <= self.create_rate_limit_requests <= 10000:
            raise ValueError("CREATE_RATE_LIMIT_REQUESTS must be between 1 and 10000")
        if not 1 <= self.create_rate_limit_window_seconds <= 86400:
            raise ValueError(
                "CREATE_RATE_LIMIT_WINDOW_SECONDS must be between 1 and 86400"
            )
        if not 1 <= self.global_create_rate_limit_requests <= 100000:
            raise ValueError(
                "GLOBAL_CREATE_RATE_LIMIT_REQUESTS must be between 1 and 100000"
            )
        if not 1 <= self.global_create_rate_limit_window_seconds <= 86400:
            raise ValueError(
                "GLOBAL_CREATE_RATE_LIMIT_WINDOW_SECONDS must be between 1 and 86400"
            )
        if not 1 <= self.activation_timeout_seconds <= 120:
            raise ValueError("ACTIVATION_TIMEOUT_SECONDS must be between 1 and 120")
        if not 0.1 <= self.activation_poll_seconds <= 10:
            raise ValueError("ACTIVATION_POLL_SECONDS must be between 0.1 and 10")
        if not 1 <= self.status_rate_limit_requests <= 10000:
            raise ValueError("STATUS_RATE_LIMIT_REQUESTS must be between 1 and 10000")
        if not 1 <= self.status_rate_limit_window_seconds <= 86400:
            raise ValueError(
                "STATUS_RATE_LIMIT_WINDOW_SECONDS must be between 1 and 86400"
            )
        if not 0 <= self.catalog_cache_ttl_seconds <= 86400:
            raise ValueError("CATALOG_CACHE_TTL_SECONDS must be between 0 and 86400")


def _load_btcpay_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"BTCPay config file does not exist: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Could not read BTCPay YAML config") from exc
    if not isinstance(raw, dict):
        raise ValueError("BTCPay YAML config must contain a mapping")
    if not {"btcpay_base", "products"}.issubset(raw):
        raise ValueError("BTCPay YAML config must define btcpay_base and products")
    if set(raw) - {"btcpay_base", "products", "daemon"}:
        raise ValueError("BTCPay YAML config contains unknown top-level fields")
    btcpay_base = raw.get("btcpay_base")
    products = raw.get("products")
    daemon = raw.get("daemon", {})
    if not isinstance(btcpay_base, dict) or not isinstance(products, dict):
        raise ValueError("BTCPay YAML config must define btcpay_base and products")
    base_url = btcpay_base.get("base_url")
    store_id = btcpay_base.get("store_id")
    api_key = btcpay_base.get("api_key")
    if not (
        isinstance(base_url, str)
        and isinstance(store_id, str)
        and isinstance(api_key, str)
        and base_url.strip()
        and store_id.strip()
        and api_key.strip()
    ):
        raise ValueError("btcpay_base must define base_url, store_id, and api_key")
    if not isinstance(daemon, dict):
        raise ValueError("daemon must be a YAML mapping")

    plans: dict[str, SubscriptionPlan] = {}
    for product_name, subscriptions in products.items():
        if not isinstance(product_name, str) or not isinstance(subscriptions, list):
            raise ValueError("products must map names to lists of subscription plans")
        for subscription in subscriptions:
            if not isinstance(subscription, dict):
                raise ValueError(f"products.{product_name} entries must be mappings")
            if set(subscription) != {"offering_id", "plan_id", "duration"}:
                raise ValueError(f"products.{product_name} entry has invalid fields")
            plan_id = subscription.get("plan_id")
            offering_id = subscription.get("offering_id")
            if not isinstance(plan_id, str) or not plan_id.strip():
                raise ValueError(f"products.{product_name} entry is missing plan_id")
            if not isinstance(offering_id, str) or not offering_id.strip():
                raise ValueError(
                    f"products.{product_name} entry is missing offering_id"
                )
            if subscription.get("duration") not in {"month", "year"}:
                raise ValueError(f"products.{product_name} entry has invalid duration")
            if plan_id in plans:
                raise ValueError(f"Duplicate subscription plan ID: {plan_id}")
            plans[plan_id] = SubscriptionPlan(offering_id=offering_id)

    portal_duration_minutes = daemon.get("portal_duration_minutes", 1440)
    if isinstance(portal_duration_minutes, bool) or not isinstance(
        portal_duration_minutes, int
    ):
        raise ValueError("daemon.portal_duration_minutes must be an integer")
    return {
        "base_url": base_url.strip(),
        "store_id": store_id.strip(),
        "api_key": api_key.strip(),
        "plans": plans,
        "portal_duration_minutes": portal_duration_minutes,
    }


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(
        self,
        request_limit: int,
        window_seconds: int,
        max_clients: int,
    ) -> None:
        self.request_limit = request_limit
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, client_ip: str, now: float | None = None) -> tuple[bool, int]:
        current = time.monotonic() if now is None else now
        refill_per_second = self.request_limit / self.window_seconds
        with self._lock:
            bucket = self._buckets.get(client_ip)
            if bucket is None:
                if len(self._buckets) >= self.max_clients:
                    expired = [
                        ip
                        for ip, entry in self._buckets.items()
                        if current - entry.updated_at >= self.window_seconds
                    ]
                    for ip in expired:
                        del self._buckets[ip]
                if len(self._buckets) >= self.max_clients:
                    return False, self.window_seconds
                bucket = _Bucket(float(self.request_limit), current)
                self._buckets[client_ip] = bucket

            bucket.tokens = min(
                float(self.request_limit),
                bucket.tokens + (current - bucket.updated_at) * refill_per_second,
            )
            bucket.updated_at = current
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True, 0
            retry_after = max(1, math.ceil((1 - bucket.tokens) / refill_per_second))
            return False, retry_after


class ApiError(Exception):
    def __init__(
        self,
        status: HTTPStatus,
        message: str,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class SubscriptionAPI:
    def __init__(self, config: ApiConfig, client: BtcpayGateway | None = None) -> None:
        self.config = config
        self.limiter = TokenBucketLimiter(
            config.rate_limit_requests,
            config.rate_limit_window_seconds,
            config.rate_limit_max_clients,
        )
        self.create_ip_limiter = TokenBucketLimiter(
            config.create_rate_limit_requests,
            config.create_rate_limit_window_seconds,
            config.rate_limit_max_clients,
        )
        self.global_create_limiter = TokenBucketLimiter(
            config.global_create_rate_limit_requests,
            config.global_create_rate_limit_window_seconds,
            1,
        )
        self.status_limiter = TokenBucketLimiter(
            config.status_rate_limit_requests,
            config.status_rate_limit_window_seconds,
            config.rate_limit_max_clients,
        )
        self._catalog_cache: tuple[float, PlanCatalogResponse] | None = None
        self._catalog_lock = threading.Lock()
        self.client = client or BtcpayClient(
            BtcpayConfig(
                base_url=config.btcpay_base_url,
                store_id=config.store_id,
                api_key=config.api_key,
                timeout_seconds=math.ceil(config.request_timeout_seconds),
            )
        )
        self._workflow_slots = threading.BoundedSemaphore(
            config.max_concurrent_workflows
        )

    def create_management_url(
        self,
        plan_id: str,
        buyer_email: str,
        client_ip: str,
    ) -> str:
        plan = self.config.plans.get(plan_id)
        if plan is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Unknown plan")
        if not self._workflow_slots.acquire(blocking=False):
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Service is busy")
        try:
            return self._create_management_url(plan_id, plan, buyer_email, client_ip)
        except Exception as exc:
            raise _upstream_error(exc) from None
        finally:
            self._workflow_slots.release()

    def get_subscription_status(
        self,
        plan_id: str,
        buyer_email: str,
    ) -> SubscriptionStatusResponse:
        plan = self.config.plans[plan_id]
        try:
            subscriber = self._matching_subscriber(plan, plan_id, buyer_email)
            return SubscriptionStatusResponse.from_subscriber(plan_id, subscriber)
        except Exception as exc:
            raise _upstream_error(exc) from None

    def get_store_info(self) -> StoreInfoResponse:
        return StoreInfoResponse(
            base_url=self.config.btcpay_base_url,
            store_id=self.config.store_id,
        )

    def get_catalog(self) -> PlanCatalogResponse:
        ttl = self.config.catalog_cache_ttl_seconds
        with self._catalog_lock:
            if self._catalog_cache is not None:
                expires_at, cached = self._catalog_cache
                if time.monotonic() < expires_at:
                    return cached
            try:
                catalog = self._load_catalog()
            except Exception as exc:
                raise _upstream_error(exc) from None
            if ttl > 0:
                self._catalog_cache = (time.monotonic() + ttl, catalog)
            return catalog

    def _load_catalog(self) -> PlanCatalogResponse:
        plans = {
            plan_id: PlanCatalogItem.from_greenfield(
                self.client.get_offering_plan(plan.offering_id, plan_id)
            )
            for plan_id, plan in self.config.plans.items()
        }
        return PlanCatalogResponse(plans=plans)

    def _matching_subscriber(
        self,
        plan: SubscriptionPlan,
        plan_id: str,
        buyer_email: str,
    ) -> dict[str, Any] | None:
        for selector in (f"Email:{buyer_email}", buyer_email):
            subscriber = self.client.get_subscriber(plan.offering_id, selector)
            if isinstance(subscriber, dict) and _subscriber_has_plan(
                subscriber, plan_id
            ):
                return subscriber
        return None

    def _create_management_url(
        self,
        plan_id: str,
        plan: SubscriptionPlan,
        buyer_email: str,
        client_ip: str,
    ) -> str:
        customer_selector = self._existing_matching_customer(plan, plan_id, buyer_email)
        if customer_selector is None:
            self._check_creation_limits(client_ip)
            checkout = self.client.create_plan_checkout(
                offering_id=plan.offering_id,
                plan_id=plan_id,
                subscriber_email=buyer_email,
                is_trial=True,
            )
            checkout_id = checkout.get("id")
            if not isinstance(checkout_id, str) or not checkout_id:
                raise RuntimeError("Checkout response did not contain an id")
            self.client.proceed_plan_checkout(checkout_id, buyer_email)
            checkout = self._wait_for_activation(checkout_id)
            subscriber = checkout.get("subscriber")
            if not isinstance(subscriber, dict) or not _subscriber_has_plan(
                subscriber, plan_id
            ):
                raise RuntimeError("Checkout activated a different plan")
            customer_selector = customer_selector_from_checkout(checkout)

        portal = self.client.create_portal_session(
            offering_id=plan.offering_id,
            customer_selector=customer_selector,
            duration_minutes=self.config.portal_duration_minutes,
        )
        management_url = portal.get("url")
        if not isinstance(management_url, str) or not _valid_http_url(management_url):
            raise RuntimeError("Portal response did not contain a valid URL")
        return management_url

    def _check_creation_limits(self, client_ip: str) -> None:
        allowed, retry_after = self.create_ip_limiter.allow(client_ip)
        if not allowed:
            raise ApiError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Subscription creation rate limit exceeded",
                retry_after,
            )
        allowed, retry_after = self.global_create_limiter.allow("all-clients")
        if not allowed:
            raise ApiError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Global subscription creation limit exceeded",
                retry_after,
            )

    def _existing_matching_customer(
        self,
        plan: SubscriptionPlan,
        plan_id: str,
        buyer_email: str,
    ) -> str | None:
        subscriber = self._matching_subscriber(plan, plan_id, buyer_email)
        if subscriber is None:
            return None
        customer = subscriber.get("customer")
        if isinstance(customer, dict):
            customer_id = customer.get("id")
            if isinstance(customer_id, str) and customer_id:
                return customer_id
        return None

    def _wait_for_activation(self, checkout_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.activation_timeout_seconds
        while True:
            checkout = self.client.get_plan_checkout(checkout_id)
            subscriber = checkout.get("subscriber")
            if checkout.get("planStarted") is True and isinstance(subscriber, dict):
                return checkout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ApiError(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    "Subscription activation timed out",
                )
            time.sleep(min(self.config.activation_poll_seconds, remaining))


def _upstream_error(exc: Exception) -> ApiError:
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, requests.Timeout):
        logger.warning("BTCPay request timed out (%s)", type(exc).__name__)
        return ApiError(HTTPStatus.GATEWAY_TIMEOUT, "Subscription service timed out")
    logger.warning("BTCPay request failed (%s)", type(exc).__name__)
    return ApiError(HTTPStatus.BAD_GATEWAY, "Subscription service unavailable")


def parse_payload(body: bytes, config: ApiConfig) -> tuple[str, str]:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Malformed JSON") from None
    if not isinstance(payload, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Request must be a JSON object")
    if set(payload) != {"plan_id", "buyerEmail"}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Request fields are invalid")
    plan_id = payload.get("plan_id")
    buyer_email = payload.get("buyerEmail")
    if not isinstance(plan_id, str) or not plan_id or len(plan_id) > MAX_PLAN_ID_LENGTH:
        raise ApiError(HTTPStatus.BAD_REQUEST, "plan_id is invalid")
    if (
        not isinstance(buyer_email, str)
        or len(buyer_email) > MAX_EMAIL_LENGTH
        or not EMAIL_PATTERN.fullmatch(buyer_email)
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "buyerEmail is invalid")
    if plan_id not in config.plans:
        raise ApiError(HTTPStatus.NOT_FOUND, "Unknown plan")
    return plan_id, buyer_email


def create_server(
    config: ApiConfig,
    client: BtcpayGateway | None = None,
) -> ThreadingHTTPServer:
    api = SubscriptionAPI(config, client)

    class Handler(SubscriptionRequestHandler):
        subscription_api = api

    server = ThreadingHTTPServer((config.bind_host, config.port), Handler)
    server.daemon_threads = True
    server.request_queue_size = config.max_concurrent_workflows
    return server


class SubscriptionRequestHandler(BaseHTTPRequestHandler):
    subscription_api: SubscriptionAPI

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.subscription_api.config.request_timeout_seconds)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path not in {CATALOG_PATH, STORE_PATH}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if not self._allow_global_request():
            return
        if self.path == STORE_PATH:
            store = self.subscription_api.get_store_info()
            self._send_json(HTTPStatus.OK, store.model_dump(mode="json"))
            return
        try:
            catalog = self.subscription_api.get_catalog()
        except ApiError as exc:
            self._send_api_error(exc)
            return
        self._send_json(HTTPStatus.OK, catalog.model_dump(mode="json"))

    def do_POST(self) -> None:
        if self.path not in {MANAGEMENT_URL_PATH, STATUS_PATH}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if not self._allow_global_request():
            return
        if self.path == STATUS_PATH and not self._allow_status_request():
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            plan_id, buyer_email = parse_payload(body, self.subscription_api.config)
            if self.path == STATUS_PATH:
                status = self.subscription_api.get_subscription_status(
                    plan_id, buyer_email
                )
                self._send_json(HTTPStatus.OK, status.model_dump(mode="json"))
                return
            management_url = self.subscription_api.create_management_url(
                plan_id,
                buyer_email,
                self.client_address[0],
            )
        except ApiError as exc:
            self._send_api_error(exc)
            return
        self._send_json(HTTPStatus.OK, {"management_url": management_url})

    def _allow_global_request(self) -> bool:
        if self.client_address[0] is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid client"})
            return False
        allowed, retry_after = self.subscription_api.limiter.allow(
            self.client_address[0]
        )
        if not allowed:
            self._send_rate_limited(retry_after)
        return allowed

    def _allow_status_request(self) -> bool:
        allowed, retry_after = self.subscription_api.status_limiter.allow(
            self.client_address[0]
        )
        if not allowed:
            self._send_rate_limited(retry_after)
        return allowed

    def _send_rate_limited(self, retry_after: int) -> None:
        self._send_json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": "Rate limit exceeded"},
            {"Retry-After": str(retry_after)},
        )

    def _send_api_error(self, exc: ApiError) -> None:
        headers = (
            {"Retry-After": str(exc.retry_after)}
            if exc.retry_after is not None
            else None
        )
        self._send_json(exc.status, {"error": exc.message}, headers)

    def _read_json_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request"})
            return None
        if self.headers.get_content_type() != "application/json":
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Expected JSON"}
            )
            return None
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = -1
        if content_length < 0:
            self._send_json(
                HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"}
            )
            return None
        if content_length > self.subscription_api.config.max_request_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request too large"}
            )
            return None
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Incomplete request"})
            return None
        return body

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("HTTP %s %s", self.command, self.path.partition("?")[0])


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    config = ApiConfig.from_env()
    server = create_server(config)
    logger.info("Subscription API listening on %s:%s", config.bind_host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _subscriber_has_plan(subscriber: dict[str, Any], plan_id: str) -> bool:
    plan = subscriber.get("plan")
    return isinstance(plan, dict) and plan.get("id") == plan_id


def _valid_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
