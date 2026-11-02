from __future__ import annotations

import json
import threading
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

from btcpay_tools.btcpay_subscription_api.main import (
    ApiConfig,
    ApiError,
    SubscriptionAPI,
    SubscriptionPlan,
    TokenBucketLimiter,
    _load_btcpay_config,
    create_server,
    parse_payload,
)
from btcpay_tools.btcpay_subscription_api.models import (
    SubscriptionPhase,
    SubscriptionStatusCode,
    SubscriptionStatusResponse,
)


def config(**overrides: Any) -> ApiConfig:
    plans = overrides.pop("plans", {"plan-monthly": SubscriptionPlan("offering")})
    values: dict[str, Any] = {
        "btcpay_base_url": "https://btcpay.example.org",
        "store_id": "store",
        "api_key": "secret-token",
        "plans": plans,
        "portal_duration_minutes": 1440,
        "bind_host": "127.0.0.1",
        "port": 0,
        "request_timeout_seconds": 2,
        "max_request_bytes": 4096,
        "max_concurrent_workflows": 2,
        "rate_limit_requests": 10,
        "rate_limit_window_seconds": 60,
        "rate_limit_max_clients": 100,
        "create_rate_limit_requests": 2,
        "create_rate_limit_window_seconds": 3600,
        "global_create_rate_limit_requests": 10,
        "global_create_rate_limit_window_seconds": 3600,
        "activation_timeout_seconds": 1,
        "activation_poll_seconds": 0.1,
        "status_rate_limit_requests": 10,
        "status_rate_limit_window_seconds": 60,
        "catalog_cache_ttl_seconds": 300,
    }
    values.update(overrides)
    return ApiConfig(**values)


class FakeClient:
    def __init__(self) -> None:
        self.subscriber: dict[str, Any] | None = None
        self.checkout: dict[str, Any] = {
            "planStarted": True,
            "subscriber": {
                "plan": {"id": "plan-monthly"},
                "customer": {"id": "customer-1"},
            },
        }
        self.portal: dict[str, Any] = {"url": "https://btcpay.example.org/portal"}
        self.calls: list[tuple[Any, ...]] = []
        self.proceed_error: Exception | None = None
        self.subscriber_entered: threading.Event | None = None
        self.release_subscriber: threading.Event | None = None
        self.plan_by_id: dict[str, dict[str, Any]] = {
            "plan-monthly": {
                "id": "plan-monthly",
                "name": "Monthly",
                "status": "Active",
                "price": "19.99",
                "currency": "USD",
                "recurringType": "Monthly",
                "gracePeriodDays": 7,
                "trialDays": 14,
                "description": "Standard monthly.",
                "memberCount": 0,
                "features": ["feature-analytics"],
            }
        }

    def get_subscriber(
        self,
        offering_id: str,
        customer_selector: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("get_subscriber", offering_id, customer_selector))
        if self.subscriber_entered is not None:
            self.subscriber_entered.set()
        if self.release_subscriber is not None:
            self.release_subscriber.wait(timeout=2)
        return self.subscriber

    def get_offering_plan(self, offering_id: str, plan_id: str) -> dict[str, Any]:
        self.calls.append(("get_offering_plan", offering_id, plan_id))
        return self.plan_by_id[plan_id]

    def create_plan_checkout(
        self,
        offering_id: str,
        plan_id: str,
        subscriber_email: str,
        is_trial: bool,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "create_plan_checkout",
                {
                    "offering_id": offering_id,
                    "plan_id": plan_id,
                    "subscriber_email": subscriber_email,
                    "is_trial": is_trial,
                },
            )
        )
        return {"id": "checkout-1"}

    def proceed_plan_checkout(
        self,
        checkout_id: str,
        subscriber_email: str,
    ) -> dict[str, Any]:
        self.calls.append(("proceed_plan_checkout", checkout_id, subscriber_email))
        if self.proceed_error:
            raise self.proceed_error
        return {}

    def get_plan_checkout(self, checkout_id: str) -> dict[str, Any]:
        self.calls.append(("get_plan_checkout", checkout_id))
        return self.checkout

    def create_portal_session(
        self,
        offering_id: str,
        customer_selector: str,
        duration_minutes: int,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "create_portal_session",
                {
                    "offering_id": offering_id,
                    "customer_selector": customer_selector,
                    "duration_minutes": duration_minutes,
                },
            )
        )
        return self.portal


def test_parse_payload_validates_schema_and_plan() -> None:
    payload = json.dumps(
        {"plan_id": "plan-monthly", "buyerEmail": "buyer@example.org"}
    ).encode()
    assert parse_payload(payload, config()) == ("plan-monthly", "buyer@example.org")

    for invalid in (
        b"[]",
        b'{"plan_id":"plan-monthly","buyerEmail":"buyer@example.org","x":1}',
        b'{"plan_id":"plan-monthly","buyerEmail":"bad"}',
        b'{"plan_id":"plan-monthly","plan_id":"plan-monthly","buyerEmail":"a@b.com"}',
        b'{"plan_id":"plan-other","buyerEmail":"buyer@example.org"}',
        b'{"plan_id":"plan-monthly","buyerEmail":"buyer@example.org","x":NaN}',
    ):
        with pytest.raises(ApiError):
            parse_payload(invalid, config())


def test_config_loader_flattens_multiple_products_into_plan_allowlist(
    tmp_path: Path,
) -> None:
    path = tmp_path / "btcpay.yaml"
    path.write_text(
        """
btcpay_base:
  base_url: https://btcpay.example.org
  pos_app_id: pos-app
  store_id: store
  api_key: secret-token
products: # Sample plan catalog
  business-plan:
    - offering_id: offering-business
      plan_id: plan-business
      pos_id: business
      trial_pos_id: business-trial
      duration: year
  notifications:
    - offering_id: offering-notifications
      plan_id: plan-notification-month
      pos_id: notification-month
      trial_pos_id: notification-month-trial
      duration: month
    - offering_id: offering-notifications
      plan_id: plan-notification-year
      pos_id: notification-year
      trial_pos_id: notification-year-trial
      duration: year
daemon: # Portal settings
  portal_duration_minutes: 720
""",
        encoding="utf-8",
    )

    loaded = _load_btcpay_config(path)

    assert loaded["base_url"] == "https://btcpay.example.org"
    assert loaded["store_id"] == "store"
    assert loaded["api_key"] == "secret-token"
    assert loaded["portal_duration_minutes"] == 720
    assert loaded["plans"] == {
        "plan-business": SubscriptionPlan("offering-business"),
        "plan-notification-month": SubscriptionPlan("offering-notifications"),
        "plan-notification-year": SubscriptionPlan("offering-notifications"),
    }


def test_config_loader_rejects_nostr_client_block(tmp_path: Path) -> None:
    path = tmp_path / "btcpay.yaml"
    path.write_text(
        """
btcpay_base:
  base_url: https://btcpay.example.org
  pos_app_id: pos-app
  store_id: store
  api_key: secret-token
products:
  business-plan:
    - offering_id: offering-business
      plan_id: plan-business
      pos_id: business
      trial_pos_id: business-trial
      duration: year
client:
  npub_bitcoin_safe_pos: npub-example
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown top-level fields"):
        _load_btcpay_config(path)


def test_config_loader_rejects_duplicate_plan_ids(tmp_path: Path) -> None:
    template_path = Path(
        "btcpay_tools/btcpay_subscription_api/btcpay_subscription.yaml.example"
    )
    raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    raw["products"]["schedule"][0]["plan_id"] = raw["products"]["business-plan"][0][
        "plan_id"
    ]
    path = tmp_path / "duplicate.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate subscription plan ID"):
        _load_btcpay_config(path)


def test_existing_matching_subscriber_issues_portal_without_checkout() -> None:
    client = FakeClient()
    client.subscriber = {
        "plan": {"id": "plan-monthly"},
        "customer": {"id": "customer-existing"},
    }

    result = SubscriptionAPI(config(), client).create_management_url(
        "plan-monthly", "buyer@example.org", "192.0.2.1"
    )

    assert result == "https://btcpay.example.org/portal"
    assert not any(call[0] == "create_plan_checkout" for call in client.calls)
    assert client.calls[-1] == (
        "create_portal_session",
        {
            "offering_id": "offering",
            "customer_selector": "customer-existing",
            "duration_minutes": 1440,
        },
    )


def test_missing_or_conflicting_subscriber_checks_out_requested_plan() -> None:
    for existing in (
        None,
        {"plan": {"id": "plan-other"}, "customer": {"id": "customer-1"}},
    ):
        client = FakeClient()
        client.subscriber = existing

        SubscriptionAPI(config(), client).create_management_url(
            "plan-monthly", "buyer@example.org", "192.0.2.1"
        )

        checkout_calls = [
            call for call in client.calls if call[0] == "create_plan_checkout"
        ]
        assert checkout_calls == [
            (
                "create_plan_checkout",
                {
                    "offering_id": "offering",
                    "plan_id": "plan-monthly",
                    "subscriber_email": "buyer@example.org",
                    "is_trial": True,
                },
            )
        ]
        assert (
            "proceed_plan_checkout",
            "checkout-1",
            "buyer@example.org",
        ) in client.calls
        assert ("get_plan_checkout", "checkout-1") in client.calls


def test_each_allowlisted_plan_uses_its_configured_offering() -> None:
    client = FakeClient()
    client.checkout["subscriber"]["plan"]["id"] = "plan-yearly"
    api = SubscriptionAPI(
        config(
            plans={
                "plan-monthly": SubscriptionPlan("offering-monthly"),
                "plan-yearly": SubscriptionPlan("offering-yearly"),
            }
        ),
        client,
    )

    api.create_management_url("plan-yearly", "buyer@example.org", "192.0.2.1")

    assert client.calls[0] == (
        "get_subscriber",
        "offering-yearly",
        "Email:buyer@example.org",
    )
    checkout = next(call for call in client.calls if call[0] == "create_plan_checkout")
    assert checkout[1]["offering_id"] == "offering-yearly"
    assert checkout[1]["plan_id"] == "plan-yearly"


def test_different_activated_plan_never_gets_portal() -> None:
    client = FakeClient()
    client.checkout = {
        "planStarted": True,
        "subscriber": {
            "plan": {"id": "plan-other"},
            "customer": {"id": "customer-1"},
        },
    }

    with pytest.raises(ApiError) as error:
        SubscriptionAPI(config(), client).create_management_url(
            "plan-monthly", "buyer@example.org", "192.0.2.1"
        )

    assert error.value.status == HTTPStatus.BAD_GATEWAY
    assert not any(call[0] == "create_portal_session" for call in client.calls)


def test_upstream_failure_does_not_leak_credentials_or_response() -> None:
    client = FakeClient()
    client.proceed_error = RuntimeError("token secret-token raw response body")

    with pytest.raises(ApiError) as error:
        SubscriptionAPI(config(), client).create_management_url(
            "plan-monthly", "buyer@example.org", "192.0.2.1"
        )

    assert error.value.message == "Subscription service unavailable"
    assert "secret-token" not in str(error.value)
    assert "raw response" not in str(error.value)


def test_timeout_and_activation_timeout_have_generic_gateway_errors() -> None:
    client = FakeClient()
    client.proceed_error = requests.Timeout("secret-token in upstream detail")
    with pytest.raises(ApiError) as upstream_error:
        SubscriptionAPI(config(), client).create_management_url(
            "plan-monthly", "buyer@example.org", "192.0.2.1"
        )
    assert upstream_error.value.status == HTTPStatus.GATEWAY_TIMEOUT
    assert "secret-token" not in str(upstream_error.value)

    client = FakeClient()
    client.checkout = {"planStarted": False}
    with pytest.raises(ApiError) as activation_error:
        SubscriptionAPI(
            config(activation_timeout_seconds=0.01, activation_poll_seconds=0.1),
            client,
        ).create_management_url("plan-monthly", "buyer@example.org", "192.0.2.1")
    assert activation_error.value.status == HTTPStatus.GATEWAY_TIMEOUT
    assert not any(call[0] == "create_portal_session" for call in client.calls)


def test_token_bucket_rate_limit_and_bounded_client_state() -> None:
    limiter = TokenBucketLimiter(request_limit=2, window_seconds=10, max_clients=1)
    assert limiter.allow("192.0.2.1", now=0) == (True, 0)
    assert limiter.allow("192.0.2.1", now=0) == (True, 0)
    assert limiter.allow("192.0.2.1", now=0) == (False, 5)
    assert limiter.allow("192.0.2.2", now=0) == (False, 10)
    assert limiter.allow("192.0.2.2", now=11) == (True, 0)


def test_global_creation_limit_applies_across_ips_but_not_existing_plans() -> None:
    client = FakeClient()
    api = SubscriptionAPI(
        config(
            global_create_rate_limit_requests=1,
            global_create_rate_limit_window_seconds=3600,
        ),
        client,
    )
    api.create_management_url("plan-monthly", "one@example.org", "192.0.2.1")

    with pytest.raises(ApiError) as error:
        api.create_management_url("plan-monthly", "two@example.org", "192.0.2.2")

    assert error.value.status == HTTPStatus.TOO_MANY_REQUESTS
    assert error.value.retry_after == 3600
    assert sum(call[0] == "create_plan_checkout" for call in client.calls) == 1

    client.subscriber = {
        "plan": {"id": "plan-monthly"},
        "customer": {"id": "customer-existing"},
    }
    assert (
        api.create_management_url("plan-monthly", "known@example.org", "192.0.2.3")
        == "https://btcpay.example.org/portal"
    )
    assert sum(call[0] == "get_subscriber" for call in client.calls) == 5
    assert sum(call[0] == "create_plan_checkout" for call in client.calls) == 1


def test_creation_limit_is_stricter_per_ip_than_check_limit() -> None:
    client = FakeClient()
    api = SubscriptionAPI(
        config(
            create_rate_limit_requests=1,
            create_rate_limit_window_seconds=3600,
        ),
        client,
    )
    api.create_management_url("plan-monthly", "one@example.org", "192.0.2.1")

    with pytest.raises(ApiError) as error:
        api.create_management_url("plan-monthly", "two@example.org", "192.0.2.1")

    assert error.value.status == HTTPStatus.TOO_MANY_REQUESTS
    assert error.value.retry_after == 3600
    assert sum(call[0] == "get_subscriber" for call in client.calls) == 4
    assert sum(call[0] == "create_plan_checkout" for call in client.calls) == 1


def test_concurrent_workflow_cap_returns_service_unavailable() -> None:
    client = FakeClient()
    client.subscriber = {
        "plan": {"id": "plan-monthly"},
        "customer": {"id": "customer-existing"},
    }
    client.subscriber_entered = threading.Event()
    client.release_subscriber = threading.Event()
    api = SubscriptionAPI(config(max_concurrent_workflows=1), client)
    errors: list[Exception] = []
    worker = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: api.create_management_url(
                "plan-monthly", "buyer@example.org", "192.0.2.1"
            ),
        )
    )
    worker.start()
    try:
        assert client.subscriber_entered.wait(timeout=2)
        with pytest.raises(ApiError) as error:
            api.create_management_url("plan-monthly", "buyer@example.org", "192.0.2.1")
        assert error.value.status == HTTPStatus.SERVICE_UNAVAILABLE
    finally:
        client.release_subscriber.set()
        worker.join(timeout=2)
    assert errors == []


def test_http_rate_limit_and_health_endpoint() -> None:
    client = FakeClient()
    server = create_server(
        config(rate_limit_requests=1, rate_limit_window_seconds=60), client
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = f"http://127.0.0.1:{server.server_port}"
        health = requests.get(f"{address}/health", timeout=2)
        assert health.status_code == HTTPStatus.OK

        payload = {"plan_id": "plan-monthly", "buyerEmail": "buyer@example.org"}
        first = requests.post(
            f"{address}/api/subscriptions/management-url", json=payload, timeout=2
        )
        limited = requests.post(
            f"{address}/api/subscriptions/management-url", json=payload, timeout=2
        )
        assert first.status_code == HTTPStatus.OK
        assert limited.status_code == HTTPStatus.TOO_MANY_REQUESTS
        assert limited.headers["Retry-After"] == "60"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_rejects_oversized_body_before_upstream_call() -> None:
    client = FakeClient()
    server = create_server(config(max_request_bytes=16), client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = requests.post(
            f"http://127.0.0.1:{server.server_port}/api/subscriptions/management-url",
            data=b" " * 17,
            headers={"Content-Type": "application/json"},
            timeout=2,
        )
        assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        assert client.calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_config_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        replace(config(), request_timeout_seconds=0).validate()
    with pytest.raises(ValueError):
        replace(config(), btcpay_base_url="file:///etc/passwd").validate()


def subscriber(
    phase: str = "Normal",
    is_active: bool = True,
    is_suspended: bool = False,
    plan_id: str = "plan-monthly",
) -> dict[str, Any]:
    return {
        "plan": {"id": plan_id},
        "customer": {"id": "customer-1"},
        "phase": phase,
        "isActive": is_active,
        "isSuspended": is_suspended,
        "periodEnd": 1800000000,
        "trialEnd": None,
        "gracePeriodEnd": None,
        "processingInvoiceId": None,
    }


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (subscriber(), SubscriptionStatusCode.ACTIVE),
        (subscriber(phase="Trial"), SubscriptionStatusCode.TRIAL),
        (subscriber(phase="Grace", is_active=True), SubscriptionStatusCode.GRACE),
        (subscriber(phase="Expired", is_active=False), SubscriptionStatusCode.EXPIRED),
        (subscriber(is_active=False), SubscriptionStatusCode.EXPIRED),
        (
            subscriber(is_suspended=True, is_active=False),
            SubscriptionStatusCode.SUSPENDED,
        ),
        (
            subscriber(phase="Expired", is_suspended=True),
            SubscriptionStatusCode.SUSPENDED,
        ),
        (subscriber(phase="Mystery"), SubscriptionStatusCode.UNKNOWN),
    ],
)
def test_status_derivation_branches(
    fixture: dict[str, Any], expected: SubscriptionStatusCode
) -> None:
    assert SubscriptionStatusCode.from_dict(fixture) == expected


def test_status_response_maps_phase_and_passes_processing_invoice() -> None:
    fixture = subscriber(phase="Trial")
    fixture["processingInvoiceId"] = "inv_88443"

    response = SubscriptionStatusResponse.from_subscriber("plan-monthly", fixture)

    assert response.phase is SubscriptionPhase.TRIAL
    assert response.status is SubscriptionStatusCode.TRIAL
    assert response.processingInvoiceId == "inv_88443"
    assert response.schema_version == 1


def test_status_for_absent_or_other_plan_subscriber_is_not_found() -> None:
    client = FakeClient()
    api = SubscriptionAPI(config(), client)

    client.subscriber = None
    assert api.get_subscription_status("plan-monthly", "buyer@example.org").status == (
        SubscriptionStatusCode.NOT_FOUND
    )

    client.subscriber = subscriber(plan_id="plan-other")
    result = api.get_subscription_status("plan-monthly", "buyer@example.org")
    assert result.status is SubscriptionStatusCode.NOT_FOUND
    assert result.phase is None


def test_status_uses_email_selector_then_raw_email() -> None:
    client = FakeClient()
    api = SubscriptionAPI(config(), client)

    api.get_subscription_status("plan-monthly", "buyer@example.org")

    assert [call[2] for call in client.calls if call[0] == "get_subscriber"] == [
        "Email:buyer@example.org",
        "buyer@example.org",
    ]


def test_catalog_returns_only_configured_plans_and_caches_within_ttl() -> None:
    client = FakeClient()
    client.plan_by_id["plan-other"] = {"id": "plan-other", "name": "Other"}
    api = SubscriptionAPI(config(), client)

    first = api.get_catalog()
    second = api.get_catalog()

    assert [plan.plan_id for plan in first.plans] == ["plan-monthly"]
    assert first.plans[0].price == "19.99"
    assert "memberCount" not in first.model_dump()["plans"][0]
    assert second == first
    assert sum(call[0] == "get_offering_plan" for call in client.calls) == 1


def test_catalog_refetches_after_ttl_expires() -> None:
    client = FakeClient()
    api = SubscriptionAPI(config(catalog_cache_ttl_seconds=0), client)

    api.get_catalog()
    api.get_catalog()

    assert sum(call[0] == "get_offering_plan" for call in client.calls) == 2


def test_catalog_upstream_failure_is_generic_gateway_error() -> None:
    client = FakeClient()

    def fail(offering_id: str, plan_id: str) -> dict[str, Any]:
        raise RuntimeError("secret-token raw body")

    client.get_offering_plan = fail  # type: ignore[method-assign]

    with pytest.raises(ApiError) as error:
        SubscriptionAPI(config(), client).get_catalog()
    assert error.value.status == HTTPStatus.BAD_GATEWAY
    assert "secret-token" not in error.value.message


def _start(server: Any) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_http_status_and_catalog_routes_and_rate_limits() -> None:
    client = FakeClient()
    client.subscriber = subscriber()
    server = create_server(
        config(status_rate_limit_requests=1, status_rate_limit_window_seconds=60),
        client,
    )
    thread = _start(server)
    try:
        address = f"http://127.0.0.1:{server.server_port}"
        status_payload = {"plan_id": "plan-monthly", "buyerEmail": "buyer@example.org"}

        catalog = requests.get(f"{address}/api/subscription-plans", timeout=2)
        assert catalog.status_code == HTTPStatus.OK
        assert catalog.json()["schema_version"] == 1
        assert catalog.json()["plans"][0]["plan_id"] == "plan-monthly"

        first = requests.post(
            f"{address}/api/subscriptions/status", json=status_payload, timeout=2
        )
        assert first.status_code == HTTPStatus.OK
        assert first.json()["status"] == "active"
        assert "management_url" not in first.json()

        limited = requests.post(
            f"{address}/api/subscriptions/status", json=status_payload, timeout=2
        )
        assert limited.status_code == HTTPStatus.TOO_MANY_REQUESTS
        assert limited.headers["Retry-After"] == "60"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_status_unknown_plan_returns_404_without_upstream_call() -> None:
    client = FakeClient()
    server = create_server(config(), client)
    thread = _start(server)
    try:
        response = requests.post(
            f"http://127.0.0.1:{server.server_port}/api/subscriptions/status",
            json={"plan_id": "plan-other", "buyerEmail": "buyer@example.org"},
            timeout=2,
        )
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert client.calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _capture_error(errors: list[Exception], operation: Any) -> None:
    try:
        operation()
    except Exception as exc:
        errors.append(exc)
