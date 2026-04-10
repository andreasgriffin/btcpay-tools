from __future__ import annotations

import json
import re
import secrets
import string
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

from btcpay_tools.btcpay_subscription_nostr.management_payload import (
    ManagementPayload,
)

import logging

logger = logging.getLogger(__file__)

ORDER_ID_PATTERN = re.compile(r"^\d{6}-[0-9a-f]{16,64}$")
SIGNED_DATA_ORDER_URL_KEY = "signed_data0"
ORIGIN_NPUB_ORDER_URL_KEY = "origin_npub"
DEFAULT_ORDER_URL = "https://btcpay-tools.invalid/subscription"


def log(message: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), message, flush=True)


@dataclass
class BtcpayConfig:
    base_url: str
    store_id: str
    api_key: str
    timeout_seconds: int = 20
    proxy_dict: dict[str, str] | None = None


@dataclass
class PosInvoiceMetadata:
    buyer_email: str
    order_url: str | None = None
    payment_request_id: str | None = None
    message_to_be_signed: str | None = None
    origin_npub: str | None = None
    pos_data: dict[str, Any] | list[Any] | None = None
    receipt_data: dict[str, Any] | list[Any] | None = None
    buyer_name: str | None = None
    buyer_address1: str | None = None
    buyer_address2: str | None = None
    buyer_city: str | None = None
    buyer_state: str | None = None
    buyer_zip: str | None = None
    buyer_country: str | None = None
    buyer_phone: str | None = None
    item_desc: str | None = None
    physical: bool | None = None
    tax_included: Decimal | str | float | int | None = None

    def query_items(self) -> list[tuple[str, str]]:
        string_fields: list[tuple[str, str | None]] = [
            (
                "orderUrl",
                _metadata_order_url(
                    self.order_url,
                    self.message_to_be_signed,
                    self.origin_npub,
                ),
            ),
            ("paymentRequestId", self.payment_request_id),
            ("buyerName", self.buyer_name),
            ("buyerEmail", self.buyer_email),
            ("buyerAddress1", self.buyer_address1),
            ("buyerAddress2", self.buyer_address2),
            ("buyerCity", self.buyer_city),
            ("buyerState", self.buyer_state),
            ("buyerZip", self.buyer_zip),
            ("buyerCountry", self.buyer_country),
            ("buyerPhone", self.buyer_phone),
            ("itemDesc", self.item_desc),
        ]
        json_fields: list[tuple[str, dict[str, Any] | list[Any] | None]] = [
            ("posData", self.pos_data),
            ("receiptData", self.receipt_data),
        ]
        items = [(key, value) for key, value in string_fields if value]
        items.extend(
            (key, json.dumps(value, separators=(",", ":")))
            for key, value in json_fields
            if value is not None
        )
        if self.physical is not None:
            items.append(("physical", "true" if self.physical else "false"))
        if self.tax_included is not None:
            items.append(("taxIncluded", str(self.tax_included)))
        return items


class BtcpayClient:
    def __init__(self, config: BtcpayConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {config.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "btcpay-tools/nostr-nip17",
            }
        )
        if config.proxy_dict:
            self.session.proxies.update(config.proxy_dict)

    def list_invoices(
        self,
        skip: int,
        take: int,
        proxy_dict: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"/api/v1/stores/{self.config.store_id}/invoices",
            query={"skip": skip, "take": take},
            proxy_dict=proxy_dict,
        )
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected invoice list response: {type(data)!r}")
        return [invoice for invoice in data if isinstance(invoice, dict)]

    def get_invoice(
        self,
        invoice_id: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/api/v1/stores/{self.config.store_id}/invoices/{invoice_id}",
            proxy_dict=proxy_dict,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected invoice response: {type(data)!r}")
        return data

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

    def get_pos_app(
        self,
        app_id: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/api/v1/apps/pos/{app_id}",
            proxy_dict=proxy_dict,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected POS app response: {type(data)!r}")
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


def generate_order_id() -> str:
    prefix = str(secrets.randbelow(900000) + 100000)
    secret = secrets.token_hex(16)
    return f"{prefix}-{secret}"


def is_secret_order_id(order_id: str) -> bool:
    return bool(ORDER_ID_PATTERN.fullmatch(order_id))


def derive_subscriber_email(message_to_be_signed: str) -> str:
    allowed = set(string.ascii_lowercase + string.digits + "-")
    local = "".join(ch for ch in message_to_be_signed.lower() if ch in allowed)
    return f"{local}@v0.bitcoin-safe.org"


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


def build_pos_base_url(base_url: str, store_id: str) -> str:
    normalized_base_url = base_url.rstrip("/")
    normalized_store_id = store_id.strip("/")
    return f"{normalized_base_url}/apps/{normalized_store_id}/pos"


def build_pos_url(
    pos_base_url: str,
    order_id: str,
    item_id: str,
    metadata: PosInvoiceMetadata | None = None,
) -> str:
    """Build a BTCPay POS URL with documented invoice metadata fields.

    Reference: https://docs.btcpayserver.org/Development/InvoiceMetadata/
    """
    parsed = urllib.parse.urlsplit(pos_base_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("orderId", order_id))
    query.append(("itemCode", item_id))
    if metadata is not None:
        query.extend(metadata.query_items())

    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def pos_app_id_from_url(pos_base_url: str) -> str:
    parsed = urllib.parse.urlsplit(pos_base_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    for index, segment in enumerate(segments):
        if segment == "apps" and index + 1 < len(segments):
            app_id = segments[index + 1]
            if app_id:
                return app_id
    raise RuntimeError(f"Could not derive BTCPay app id from URL: {pos_base_url}")


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, InvalidOperation, ValueError):
        return None


def submit_pos_purchase(
    pos_base_url: str,
    order_id: str,
    item_id: str,
    metadata: PosInvoiceMetadata | None = None,
    timeout_seconds: int = 20,
    proxy_dict: dict[str, str] | None = None,
) -> str:
    pos_post_url = build_pos_url(
        pos_base_url=pos_base_url,
        order_id=order_id,
        item_id=item_id,
        metadata=metadata,
    )
    form_data: dict[str, str] = {"choiceKey": item_id}
    if metadata and metadata.buyer_email:
        form_data["buyerEmail"] = metadata.buyer_email
    response = requests.post(
        pos_post_url,
        data=form_data,
        allow_redirects=False,
        timeout=timeout_seconds,
        proxies=proxy_dict,
    )
    if response.status_code not in {200, 302, 303}:
        body = response.text[:800].replace("\n", " ")
        raise RuntimeError(f"BTCPay PoS purchase failed: {response.status_code} {body}")
    location = response.headers.get("Location")
    if not location:
        return pos_post_url
    return urllib.parse.urljoin(pos_post_url, location)


def invoice_order_id(invoice: dict[str, Any]) -> str | None:
    metadata = invoice.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("orderId")
    if isinstance(value, str) and value:
        return value
    return None


def invoice_buyer_email(invoice: dict[str, Any]) -> str | None:
    metadata = invoice.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("buyerEmail")
    if isinstance(value, str) and value:
        return value
    order_url = metadata.get("orderUrl")
    if isinstance(order_url, str) and order_url:
        parsed = urllib.parse.urlsplit(order_url)
        query = urllib.parse.parse_qs(parsed.query)
        buyer_email = query.get("buyerEmail")
        if buyer_email:
            first_value = buyer_email[0]
            if first_value:
                return first_value
    return None


def invoice_item_ids(invoice: dict[str, Any]) -> set[str]:
    metadata = invoice.get("metadata")
    if not isinstance(metadata, dict):
        return set()

    item_ids: set[str] = set()

    item_code = metadata.get("itemCode")
    if isinstance(item_code, str) and item_code:
        item_ids.add(item_code)

    pos_data = metadata.get("posData")
    if not isinstance(pos_data, dict):
        return item_ids

    cart = pos_data.get("cart")
    if not isinstance(cart, list):
        return item_ids

    for entry in cart:
        if not isinstance(entry, dict):
            continue
        value = entry.get("id")
        if isinstance(value, str) and value:
            item_ids.add(value)

    return item_ids


def _metadata_order_url(
    order_url: str | None,
    message_to_be_signed: str | None,
    origin_npub: str | None,
) -> str | None:
    if not message_to_be_signed and not origin_npub:
        return order_url
    base_url = order_url or DEFAULT_ORDER_URL
    parsed = urllib.parse.urlsplit(base_url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if key not in {SIGNED_DATA_ORDER_URL_KEY, ORIGIN_NPUB_ORDER_URL_KEY}
    ]
    if message_to_be_signed:
        query.append((SIGNED_DATA_ORDER_URL_KEY, message_to_be_signed))
    if origin_npub:
        query.append((ORIGIN_NPUB_ORDER_URL_KEY, origin_npub))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def _metadata_value_from_order_url(order_url: str | None, key: str) -> str | None:
    if not isinstance(order_url, str) or not order_url:
        return None
    parsed = urllib.parse.urlsplit(order_url)
    query = urllib.parse.parse_qs(parsed.query)
    values = query.get(key)
    if values:
        value = values[0]
        if value:
            return value
    nested_order_urls = query.get("orderUrl")
    if not nested_order_urls:
        return None
    return _metadata_value_from_order_url(nested_order_urls[0], key)


def metadata_message_to_be_signed(
    metadata: PosInvoiceMetadata | None,
) -> str | None:
    if metadata is None:
        return None
    value = metadata.message_to_be_signed
    if isinstance(value, str) and value:
        return value
    return None


def invoice_message_to_be_signed(invoice: dict[str, Any]) -> str | None:
    metadata = invoice.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return _metadata_value_from_order_url(
        metadata.get("orderUrl"),
        SIGNED_DATA_ORDER_URL_KEY,
    )


def invoice_origin_npub(invoice: dict[str, Any]) -> str | None:
    metadata = invoice.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return _metadata_value_from_order_url(
        metadata.get("orderUrl"),
        ORIGIN_NPUB_ORDER_URL_KEY,
    )


def invoice_matches_item(invoice: dict[str, Any], item_id: str) -> bool:
    return item_id in invoice_item_ids(invoice)


def invoice_is_settled(invoice: dict[str, Any]) -> bool:
    status = str(invoice.get("status") or "").lower()
    additional_status = str(invoice.get("additionalStatus") or "").lower()
    return status == "settled" and additional_status in {"", "none", "paid"}


def invoice_is_terminal(invoice: dict[str, Any]) -> bool:
    status = str(invoice.get("status") or "").lower()
    return status in {"invalid", "expired"}


@dataclass
class ProcessedOrderState:
    product_id: str
    plan_id: str
    customer_selector: str
    portal_url: str
    origin_npub: str | None = None
    checkout_id: str | None = None
    delivered_at: int | None = None
    last_delivery_attempt_at: int | None = None
    delivery_attempts: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> ProcessedOrderState | None:
        if not isinstance(raw, dict):
            return None
        customer_selector = raw.get("customer_selector")
        portal_url = raw.get("portal_url")
        product_id = raw.get("product_id")
        plan_id = raw.get("plan_id")
        if not isinstance(product_id, str) or not product_id:
            return None
        if not isinstance(plan_id, str) or not plan_id:
            return None
        if not isinstance(customer_selector, str) or not customer_selector:
            return None
        if not isinstance(portal_url, str) or not portal_url:
            return None
        origin_npub_raw = raw.get("origin_npub")
        origin_npub = origin_npub_raw if isinstance(origin_npub_raw, str) else None
        checkout_id_raw = raw.get("checkout_id")
        checkout_id = checkout_id_raw if isinstance(checkout_id_raw, str) else None
        delivered_at_raw = raw.get("delivered_at")
        delivered_at = int(delivered_at_raw) if delivered_at_raw else None
        last_delivery_attempt_at_raw = raw.get("last_delivery_attempt_at")
        last_delivery_attempt_at = (
            int(last_delivery_attempt_at_raw) if last_delivery_attempt_at_raw else None
        )
        delivery_attempts = int(raw.get("delivery_attempts") or 0)
        return cls(
            product_id=product_id,
            plan_id=plan_id,
            customer_selector=customer_selector,
            portal_url=portal_url,
            origin_npub=origin_npub,
            checkout_id=checkout_id,
            delivered_at=delivered_at,
            last_delivery_attempt_at=last_delivery_attempt_at,
            delivery_attempts=delivery_attempts,
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "product_id": self.product_id,
            "plan_id": self.plan_id,
            "customer_selector": self.customer_selector,
            "portal_url": self.portal_url,
            "delivery_attempts": self.delivery_attempts,
        }
        if self.origin_npub:
            payload["origin_npub"] = self.origin_npub
        if self.checkout_id:
            payload["checkout_id"] = self.checkout_id
        if self.delivered_at is not None:
            payload["delivered_at"] = self.delivered_at
        if self.last_delivery_attempt_at is not None:
            payload["last_delivery_attempt_at"] = self.last_delivery_attempt_at
        return payload

    def is_delivered(self) -> bool:
        return self.delivered_at is not None

    def delivery_payload(self) -> ManagementPayload:
        return ManagementPayload(management_url=self.portal_url)


@dataclass
class PendingInvoiceState:
    invoice_id: str
    order_id: str
    product_id: str
    plan_id: str
    created_time: int

    @classmethod
    def from_json(cls, raw: Any) -> PendingInvoiceState | None:
        if not isinstance(raw, dict):
            return None
        invoice_id = raw.get("invoice_id")
        order_id = raw.get("order_id")
        product_id = raw.get("product_id")
        plan_id = raw.get("plan_id")
        if not isinstance(invoice_id, str) or not invoice_id:
            return None
        if not isinstance(order_id, str) or not order_id:
            return None
        if not isinstance(product_id, str) or not product_id:
            return None
        if not isinstance(plan_id, str) or not plan_id:
            return None
        return cls(
            invoice_id=invoice_id,
            order_id=order_id,
            product_id=product_id,
            plan_id=plan_id,
            created_time=int(raw.get("created_time") or 0),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "order_id": self.order_id,
            "product_id": self.product_id,
            "plan_id": self.plan_id,
            "created_time": self.created_time,
        }


class PersistentState:
    def __init__(self, path: Path):
        self.path = path
        self.processed_orders: dict[str, ProcessedOrderState] = {}
        self.pending_invoices: dict[str, PendingInvoiceState] = {}
        self.last_seen_created_time: int = 0

    def load(self) -> None:
        if not self.path.exists():
            return
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return
        raw = json.loads(text)
        if not isinstance(raw, dict):
            return
        processed = raw.get("processed_orders")
        if isinstance(processed, dict):
            self.processed_orders = {
                str(key): order_state
                for key, value in processed.items()
                for order_state in [ProcessedOrderState.from_json(value)]
                if order_state is not None
            }
        pending = raw.get("pending_invoices")
        if isinstance(pending, dict):
            self.pending_invoices = {
                str(key): invoice_state
                for key, value in pending.items()
                for invoice_state in [PendingInvoiceState.from_json(value)]
                if invoice_state is not None
            }
        self.last_seen_created_time = int(raw.get("last_seen_created_time") or 0)

    def save(self) -> None:
        payload = {
            "processed_orders": {
                order_id: order_state.to_json()
                for order_id, order_state in self.processed_orders.items()
            },
            "pending_invoices": {
                invoice_id: invoice_state.to_json()
                for invoice_id, invoice_state in self.pending_invoices.items()
            },
            "last_seen_created_time": self.last_seen_created_time,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
