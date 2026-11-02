from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class BtcpayConfig:
    base_url: str
    store_id: str
    api_key: str
    timeout_seconds: int = 20
    proxy_dict: dict[str, str] | None = None


class BtcpayClient:
    """Minimal Greenfield client for the subscription API."""

    def __init__(self, config: BtcpayConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {config.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "btcpay-tools/subscription-api",
            }
        )
        if config.proxy_dict:
            self.session.proxies.update(config.proxy_dict)

    def create_plan_checkout(
        self,
        offering_id: str,
        plan_id: str,
        subscriber_email: str,
        is_trial: bool,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "storeId": self.config.store_id,
            "offeringId": offering_id,
            "planId": plan_id,
            "newSubscriberEmail": subscriber_email,
            "isTrial": is_trial,
            "metadata": {"source": "pos-nostr"},
        }
        return self._request(
            "POST",
            "/api/v1/plan-checkout",
            payload=payload,
            proxy_dict=proxy_dict,
        )

    def get_offering_plan(
        self,
        offering_id: str,
        plan_id: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/api/v1/stores/{self.config.store_id}/offerings/"
            f"{offering_id}/plans/{plan_id}",
            proxy_dict=proxy_dict,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected plan response: {type(data)!r}")
        return data

    def proceed_plan_checkout(
        self,
        checkout_id: str,
        subscriber_email: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/plan-checkout/{checkout_id}",
            query={"email": subscriber_email},
            proxy_dict=proxy_dict,
        )

    def get_plan_checkout(
        self,
        checkout_id: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/api/v1/plan-checkout/{checkout_id}",
            proxy_dict=proxy_dict,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected checkout response: {type(data)!r}")
        return data

    def create_portal_session(
        self,
        offering_id: str,
        customer_selector: str,
        duration_minutes: int = 1440,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "storeId": self.config.store_id,
            "offeringId": offering_id,
            "customerSelector": customer_selector,
            "durationMinutes": duration_minutes,
        }
        return self._request(
            "POST",
            "/api/v1/subscriber-portal",
            payload=payload,
            proxy_dict=proxy_dict,
        )

    def get_subscriber(
        self,
        offering_id: str,
        customer_selector: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            data = self._request(
                "GET",
                f"/api/v1/stores/{self.config.store_id}/offerings/"
                f"{offering_id}/subscribers/"
                f"{urllib.parse.quote(customer_selector, safe='')}",
                proxy_dict=proxy_dict,
            )
        except RuntimeError as exc:
            if " 404 " in str(exc):
                return None
            raise
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected subscriber response: {type(data)!r}")
        return data

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        proxy_dict: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.config.base_url.rstrip('/')}{path}"
        proxies = proxy_dict if proxy_dict is not None else self.config.proxy_dict
        response = self.session.request(
            method=method,
            url=url,
            json=payload,
            params=query,
            timeout=self.config.timeout_seconds,
            proxies=proxies,
        )
        if response.status_code >= 400:
            body = response.text[:800].replace("\n", " ")
            raise RuntimeError(f"{method} {url} failed: {response.status_code} {body}")
        return response.json()


def customer_selector_from_checkout(checkout: dict[str, Any]) -> str:
    subscriber = checkout.get("subscriber")
    if not isinstance(subscriber, dict):
        raise RuntimeError("Checkout did not return a subscriber")
    customer = subscriber.get("customer")
    if not isinstance(customer, dict):
        raise RuntimeError("Checkout subscriber did not contain a customer")
    customer_id = customer.get("id")
    if not isinstance(customer_id, str) or not customer_id:
        raise RuntimeError("Checkout customer did not contain an id")
    return customer_id
