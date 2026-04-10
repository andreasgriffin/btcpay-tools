from __future__ import annotations

import asyncio
import json
import re
import time
import webbrowser
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Callable, TypeVar

import requests
from bitcoin_safe_lib.async_tools.loop_in_thread import LoopInThread

from btcpay_tools.config import SubscriptionProduct
from btcpay_tools.btcpay_subscription_nostr.core import (
    BtcpayClient,
    PendingInvoiceState,
    PosInvoiceMetadata,
    PersistentState,
    ProcessedOrderState,
    customer_selector_from_checkout,
    derive_subscriber_email,
    generate_order_id,
    invoice_buyer_email,
    invoice_message_to_be_signed,
    invoice_origin_npub,
    invoice_is_settled,
    invoice_is_terminal,
    invoice_item_ids,
    invoice_order_id,
    is_secret_order_id,
    log,
    parse_decimal,
    pos_app_id_from_url,
    submit_pos_purchase,
)
from btcpay_tools.btcpay_subscription_nostr.management_payload import (
    ManagementPayload,
)
from btcpay_tools.btcpay_subscription_nostr.nostr_transport import (
    Nip17Transport,
)
import logging

logger = logging.getLogger(__file__)

MAX_NETWORK_TIMEOUT_SECONDS = 60
T = TypeVar("T")


class SubscriptionManagementPhase(str, Enum):
    NORMAL = "normal"
    TRIAL = "trial"
    GRACE = "grace"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class SubscriptionManagementStatusCode(str, Enum):
    ACTIVE = "active"
    TRIAL = "trial"
    GRACE = "grace"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    PAYMENT_DUE = "payment_due"
    UPGRADE_REQUIRED = "upgrade_required"
    PENDING_INVOICE = "pending_invoice"
    NOT_FOUND = "not_found"
    SESSION_EXPIRED = "session_expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SubscriptionManagementStatus:
    status: SubscriptionManagementStatusCode
    phase: SubscriptionManagementPhase
    is_active: bool | None
    is_suspended: bool
    pending_invoice: bool = False
    payment_due: bool = False
    upgrade_required: bool = False
    auto_renew: bool | None = None
    http_status: int | None = None


def _invoice_created_time(invoice: dict[str, object]) -> int:
    created_time = invoice.get("createdTime")
    if isinstance(created_time, bool):
        return int(created_time)
    if isinstance(created_time, int):
        return created_time
    if isinstance(created_time, float):
        return int(created_time)
    if isinstance(created_time, str):
        return int(created_time)
    return 0


def _validated_timeout_seconds(timeout_seconds: int) -> int:
    if 1 <= timeout_seconds <= MAX_NETWORK_TIMEOUT_SECONDS:
        return timeout_seconds
    raise ValueError(
        f"timeout_seconds must be between 1 and {MAX_NETWORK_TIMEOUT_SECONDS}"
    )


async def _run_with_loop_in_thread(
    loop_in_thread: LoopInThread,
    func: Callable[..., T],
    *args: Any,
) -> T:
    async def runner() -> T:
        return func(*args)

    future = loop_in_thread.run_background(runner())
    return await asyncio.wrap_future(future)


class SubscriptionManagementClient:
    @dataclass(frozen=True)
    class _PlanHeadingBadge:
        theme: str | None
        has_title: bool

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._stack: list[str] = []
            self._h5_depths: list[int] = []
            self._plan_heading_depth: int | None = None
            self._subscriber_status_depth: int | None = None

            self.saw_portal_marker = False
            self.subscriber_status_theme: str | None = None
            self.subscriber_status_has_unsuspend = False
            self.plan_heading_badges: list[
                SubscriptionManagementClient._PlanHeadingBadge
            ] = []
            self.has_pay_command = False
            self.has_migrate_command = False
            self.auto_renew: bool | None = None

        def handle_starttag(
            self,
            tag: str,
            attrs: list[tuple[str, str | None]],
        ) -> None:
            self._stack.append(tag)
            depth = len(self._stack)
            attr_map = {key: value for key, value in attrs}
            classes = SubscriptionManagementClient._classes(attr_map)

            if tag == "h5":
                self._h5_depths.append(depth)

            if attr_map.get("data-testid") == "plan-name" and self._h5_depths:
                self._plan_heading_depth = self._h5_depths[-1]
                self.saw_portal_marker = True

            if "subscriber-status" in classes:
                self._subscriber_status_depth = depth
                self.subscriber_status_theme = SubscriptionManagementClient._theme(
                    classes
                )
                self.saw_portal_marker = True

            if (
                self._plan_heading_depth is not None
                and depth > self._plan_heading_depth
            ):
                if tag == "span" and "badge" in classes:
                    self.plan_heading_badges.append(
                        SubscriptionManagementClient._PlanHeadingBadge(
                            theme=SubscriptionManagementClient._theme(classes),
                            has_title=bool(attr_map.get("title")),
                        )
                    )

            command = attr_map.get("name"), attr_map.get("value")
            if command == ("command", "pay"):
                self.has_pay_command = True
            if command == ("command", "migrate"):
                self.has_migrate_command = True
            if command == ("command", "unsuspend") and self._subscriber_status_depth:
                self.subscriber_status_has_unsuspend = True

            if tag == "input" and attr_map.get("id") == "autoRenewal":
                self.auto_renew = "checked" in attr_map
                self.saw_portal_marker = True

        def handle_endtag(self, tag: str) -> None:
            depth = len(self._stack)

            if (
                self._subscriber_status_depth is not None
                and depth == self._subscriber_status_depth
            ):
                self._subscriber_status_depth = None

            if (
                self._plan_heading_depth is not None
                and depth == self._plan_heading_depth
            ):
                self._plan_heading_depth = None

            if tag == "h5" and self._h5_depths and self._h5_depths[-1] == depth:
                self._h5_depths.pop()

            if self._stack:
                self._stack.pop()

    def __init__(
        self,
        timeout_seconds: int = 20,
        proxy_dict: dict[str, str] | None = None,
        loop_in_thread: LoopInThread | None = None,
    ) -> None:
        self.timeout_seconds = _validated_timeout_seconds(timeout_seconds)
        self.proxy_dict = proxy_dict
        self._owns_loop_in_thread = loop_in_thread is None
        self.loop_in_thread = loop_in_thread or LoopInThread()
        self.session = requests.Session()
        if proxy_dict:
            self.session.proxies.update(proxy_dict)

    def close(self) -> None:
        if self._owns_loop_in_thread:
            self.loop_in_thread.stop()

    @staticmethod
    def _classes(attrs: dict[str, str | None]) -> set[str]:
        raw = attrs.get("class") or ""
        return {part for part in raw.split() if part}

    @staticmethod
    def _theme(classes: set[str]) -> str | None:
        for css_class in classes:
            match = re.fullmatch(r"text-bg-([a-z0-9_-]+)", css_class)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _phase_from_parser(
        parser: _Parser,
    ) -> SubscriptionManagementPhase:
        for badge in parser.plan_heading_badges:
            if badge.has_title:
                continue
            if badge.theme == "info":
                return SubscriptionManagementPhase.TRIAL
            if badge.theme == "warning":
                return SubscriptionManagementPhase.GRACE
        if parser.saw_portal_marker:
            return SubscriptionManagementPhase.NORMAL
        return SubscriptionManagementPhase.UNKNOWN

    @classmethod
    def parse_management_page_status(
        cls,
        html: str,
    ) -> SubscriptionManagementStatus:
        parser = cls._Parser()
        parser.feed(html)
        parser.close()

        if not parser.saw_portal_marker:
            return SubscriptionManagementStatus(
                status=SubscriptionManagementStatusCode.UNKNOWN,
                phase=SubscriptionManagementPhase.UNKNOWN,
                is_active=None,
                is_suspended=False,
            )

        phase = cls._phase_from_parser(parser)
        pending_invoice = any(
            badge.has_title and badge.theme == "warning"
            for badge in parser.plan_heading_badges
        )
        is_suspended = parser.subscriber_status_has_unsuspend
        is_active = (
            True
            if parser.subscriber_status_theme == "success"
            else False
            if parser.subscriber_status_theme == "danger"
            else None
        )
        payment_due = parser.has_pay_command and is_active is not False
        upgrade_required = parser.has_migrate_command and is_active is not False

        if is_suspended:
            status = SubscriptionManagementStatusCode.SUSPENDED
        elif is_active is False or phase == SubscriptionManagementPhase.EXPIRED:
            status = SubscriptionManagementStatusCode.EXPIRED
        elif phase == SubscriptionManagementPhase.TRIAL:
            status = SubscriptionManagementStatusCode.TRIAL
        elif phase == SubscriptionManagementPhase.GRACE:
            status = SubscriptionManagementStatusCode.GRACE
        elif upgrade_required:
            status = SubscriptionManagementStatusCode.UPGRADE_REQUIRED
        elif payment_due:
            status = SubscriptionManagementStatusCode.PAYMENT_DUE
        elif pending_invoice:
            status = SubscriptionManagementStatusCode.PENDING_INVOICE
        elif is_active is True or phase == SubscriptionManagementPhase.NORMAL:
            status = SubscriptionManagementStatusCode.ACTIVE
        else:
            status = SubscriptionManagementStatusCode.UNKNOWN

        if status == SubscriptionManagementStatusCode.EXPIRED:
            phase = SubscriptionManagementPhase.EXPIRED

        return SubscriptionManagementStatus(
            status=status,
            phase=phase,
            is_active=is_active,
            is_suspended=is_suspended,
            pending_invoice=pending_invoice,
            payment_due=payment_due,
            upgrade_required=upgrade_required,
            auto_renew=parser.auto_renew,
        )

    def _get_management_status(
        self,
        management_url: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> SubscriptionManagementStatus:
        proxies = proxy_dict if proxy_dict is not None else self.proxy_dict
        response = self.session.get(
            management_url,
            timeout=self.timeout_seconds,
            proxies=proxies,
        )
        if response.status_code == 404:
            return SubscriptionManagementStatus(
                status=SubscriptionManagementStatusCode.NOT_FOUND,
                phase=SubscriptionManagementPhase.UNKNOWN,
                is_active=None,
                is_suspended=False,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            body = response.text[:800].replace("\n", " ")
            raise RuntimeError(
                f"Management URL fetch failed: {response.status_code} {body}"
            )
        return self.parse_management_page_status(response.text)

    async def get_management_status(
        self,
        management_url: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> SubscriptionManagementStatus:
        return await _run_with_loop_in_thread(
            self.loop_in_thread,
            self._get_management_status,
            management_url,
            proxy_dict,
        )


@dataclass
class PurchaseSession:
    order_id: str
    receipt_url: str
    origin_npub: str
    receiver_nsec: str
    management_payload: ManagementPayload | None = None

    @property
    def management_url(self) -> str | None:
        if self.management_payload is None:
            return None
        return self.management_payload.management_url


class SubscriptionPurchaseClient:
    def __init__(
        self,
        pos_base_url: str,
        pos_item_id: str,
        metadata: PosInvoiceMetadata,
        npub_bitcoin_safe_pos: str,
        timeout_seconds: int = 20,
        proxy_dict: dict[str, str] | None = None,
        loop_in_thread: LoopInThread | None = None,
    ) -> None:
        self.pos_base_url = pos_base_url
        self.pos_item_id = pos_item_id
        self.metadata = metadata
        self.timeout_seconds = _validated_timeout_seconds(timeout_seconds)
        self.proxy_dict = proxy_dict
        self.npub_bitcoin_safe_pos = npub_bitcoin_safe_pos
        self._owns_loop_in_thread = loop_in_thread is None
        self.loop_in_thread = loop_in_thread or LoopInThread()
        self.transport = Nip17Transport(loop_in_thread=self.loop_in_thread)
        self.management_client = SubscriptionManagementClient(
            timeout_seconds=timeout_seconds,
            proxy_dict=proxy_dict,
            loop_in_thread=self.loop_in_thread,
        )

    def close(self) -> None:
        if self._owns_loop_in_thread:
            self.loop_in_thread.stop()

    def _start_purchase(
        self,
        item_id: str | None = None,
        proxy_dict: dict[str, str] | None = None,
    ) -> PurchaseSession:
        order_id = generate_order_id()
        reply_identity = self.transport.generate_identity()
        buyer_email = self.metadata.buyer_email or derive_subscriber_email(
            message_to_be_signed=self.metadata.message_to_be_signed or order_id
        )
        metadata = replace(
            self.metadata,
            buyer_email=buyer_email,
            origin_npub=reply_identity.npub,
        )
        receipt_url = submit_pos_purchase(
            pos_base_url=self.pos_base_url,
            order_id=order_id,
            item_id=item_id or self.pos_item_id,
            metadata=metadata,
            timeout_seconds=self.timeout_seconds,
            proxy_dict=proxy_dict if proxy_dict is not None else self.proxy_dict,
        )
        return PurchaseSession(
            order_id=order_id,
            receipt_url=receipt_url,
            origin_npub=reply_identity.npub,
            receiver_nsec=reply_identity.nsec,
        )

    async def start_purchase(
        self,
        item_id: str | None = None,
        proxy_dict: dict[str, str] | None = None,
    ) -> PurchaseSession:
        return await _run_with_loop_in_thread(
            self.loop_in_thread,
            self._start_purchase,
            item_id,
            proxy_dict,
        )

    async def wait_for_management_payload(
        self,
        session: PurchaseSession,
        open_management: bool = False,
    ) -> ManagementPayload:
        log(f"Waiting for management URL over Nostr for order {session.order_id}")
        payload_text = await asyncio.wrap_future(
            self.transport.receive_text_background(
                receiver_nsec=session.receiver_nsec,
                expected_sender_npub=self.npub_bitcoin_safe_pos,
                timeout_seconds=self.timeout_seconds,
            )
        )
        payload = ManagementPayload.from_json(payload_text_to_json(payload_text))
        if open_management:
            webbrowser.open(payload.management_url)
        return payload

    async def get_management_status(
        self,
        management_url: str,
        proxy_dict: dict[str, str] | None = None,
    ) -> SubscriptionManagementStatus:
        return await self.management_client.get_management_status(
            management_url=management_url,
            proxy_dict=proxy_dict,
        )

    async def start_and_wait(self, open_management: bool = False) -> PurchaseSession:
        session = await self.start_purchase()
        session.management_payload = await self.wait_for_management_payload(
            session=session,
            open_management=open_management,
        )
        return session


class SubscriptionDaemon:
    """
    SubscriptionDaemon

    Reusing an existing subscriber by email is enabled by default and is only
    safe when the email is non-guessable, server-derived, and exactly matches
    derive_subscriber_email.
    """

    def __init__(
        self,
        btcpay: BtcpayClient,
        state: PersistentState,
        products: dict[str, SubscriptionProduct],
        pos_base_url: str | None,
        portal_duration_minutes: int,
        delivery_timeout_seconds: int,
        max_delivery_attempts: int,
        initial_lookback_seconds: int,
        reuse_existing_subscriber_by_email: bool = True,
        sender_nsec: str | None = None,
        transport: Nip17Transport | None = None,
    ) -> None:
        self.btcpay = btcpay
        self.state = state
        self.products = products
        self.product_ids_by_trial_pos_id = {
            product.trial_pos_id: product_id
            for product_id, product in self.products.items()
        }
        self.products_by_trial_pos_id = {
            product.trial_pos_id: product for product in self.products.values()
        }
        self.pos_base_url = pos_base_url
        self.portal_duration_minutes = portal_duration_minutes
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.max_delivery_attempts = max(0, max_delivery_attempts)
        self.initial_lookback_seconds = initial_lookback_seconds
        self.reuse_existing_subscriber_by_email = reuse_existing_subscriber_by_email
        self.sender_nsec = sender_nsec
        self.transport = transport or Nip17Transport()
        self.delivery_futures: dict[str, Future[bool]] = {}
        self.startup_initialized = False

    def run_forever(self, poll_seconds: int, page_size: int, max_pages: int) -> None:
        while True:
            try:
                self.poll_once(page_size=page_size, max_pages=max_pages)
            except Exception as exc:
                log(f"WARN: poll failed: {exc}")
            time.sleep(max(1, poll_seconds))

    def poll_once(self, page_size: int, max_pages: int) -> None:
        if not self.startup_initialized:
            self._initialize_startup_state(page_size=page_size, max_pages=max_pages)
            self.startup_initialized = True
            return

        self._refresh_pending_invoices()
        self._deliver_pending_orders()

        watermark = self.state.last_seen_created_time
        newest_created_time = watermark
        scanned = 0
        matched = 0
        reached_watermark = False

        for page_index in range(max(1, max_pages)):
            invoices = self.btcpay.list_invoices(
                skip=page_index * page_size,
                take=page_size,
            )
            if not invoices:
                break
            for invoice in invoices:
                scanned += 1
                created_time = _invoice_created_time(invoice)
                newest_created_time = max(newest_created_time, created_time)
                if created_time < watermark:
                    reached_watermark = True
                    break
                product = self._product_for_invoice(invoice)
                if product is None:
                    continue
                order_id = str(invoice_order_id(invoice))
                if order_id in self.state.processed_orders:
                    self.state.pending_invoices.pop(str(invoice.get("id") or ""), None)
                    continue
                if invoice_is_settled(invoice):
                    matched += 1
                    full_invoice = self._full_invoice(invoice)
                    self.process_order(
                        order_id,
                        product=product,
                        subscriber_email=self._require_invoice_buyer_email(
                            full_invoice
                        ),
                        invoice=full_invoice,
                    )
                    continue
                self._remember_pending_invoice(invoice, product=product)
            if reached_watermark or len(invoices) < page_size:
                break

        if newest_created_time > watermark:
            self.state.last_seen_created_time = newest_created_time
            self.state.save()

        pending = len(self.state.pending_invoices)
        log(
            f"Poll complete: scanned {scanned} invoice(s), matched {matched}, pending settlement {pending}"
        )

    def process_order(
        self,
        order_id: str,
        product: SubscriptionProduct,
        subscriber_email: str,
        invoice: dict[str, object] | None = None,
    ) -> str:
        if order_id in self.state.processed_orders:
            portal_url = self.state.processed_orders[order_id].portal_url
            log(f"Order {order_id} already processed")
            self._deliver_order(order_id, self.state.processed_orders[order_id])
            return portal_url

        message_to_be_signed = self._message_to_be_signed(invoice)
        can_reuse_existing_subscriber = self._can_reuse_existing_subscriber(
            subscriber_email=subscriber_email,
            message_to_be_signed=message_to_be_signed,
        )
        if can_reuse_existing_subscriber:
            existing_selector = self._find_existing_customer_selector(
                product,
                subscriber_email,
            )
            if existing_selector:
                log(
                    f"Reusing existing subscriber for {order_id} via "
                    f"{existing_selector}"
                )
                return self._create_and_send_portal(
                    order_id,
                    product,
                    existing_selector,
                    origin_npub=self._origin_npub_from_invoice(invoice),
                )

        log(f"Creating subscription for {order_id} with {subscriber_email}")
        checkout = self.btcpay.create_plan_checkout(
            offering_id=product.offering_id,
            plan_id=product.plan_id,
            subscriber_email=subscriber_email,
            is_trial=True,
        )
        checkout_id = str(checkout["id"])
        self.btcpay.proceed_plan_checkout(checkout_id, subscriber_email)
        try:
            activated = self._wait_for_activated_checkout(checkout_id)
            selector = customer_selector_from_checkout(activated)
        except RuntimeError:
            # The earlier branch only skips checkout creation when a matching
            # subscriber already exists. This fallback handles a different case:
            # checkout activation may have succeeded server-side, but BTCPay did
            # not return the subscriber details before the polling timeout.
            if not can_reuse_existing_subscriber:
                raise
            existing_selector = self._find_existing_customer_selector(
                product, subscriber_email
            )
            if not existing_selector:
                raise
            log(
                "Activation did not return a subscriber immediately; "
                f"recovering via existing selector {existing_selector}"
            )
            selector = existing_selector
        return self._create_and_send_portal(
            order_id=order_id,
            product=product,
            customer_selector=selector,
            checkout_id=checkout_id,
            origin_npub=self._origin_npub_from_invoice(invoice),
        )

    def _initialize_startup_state(self, page_size: int, max_pages: int) -> None:
        self._warn_on_product_price_mismatches()
        newest_created_time = int(time.time())
        scanned = 0
        tracked_pending = 0

        for page_index in range(max(1, max_pages)):
            invoices = self.btcpay.list_invoices(
                skip=page_index * page_size,
                take=page_size,
            )
            if not invoices:
                break
            for invoice in invoices:
                scanned += 1
                created_time = _invoice_created_time(invoice)
                newest_created_time = max(newest_created_time, created_time)
                product = self._product_for_invoice(invoice)
                if product is None:
                    continue
                if invoice_is_settled(invoice):
                    continue
                self._remember_pending_invoice(invoice, product=product)
                tracked_pending += 1
            if len(invoices) < page_size:
                break

        ignored_pending = 0
        for order_state in self.state.processed_orders.values():
            if order_state.is_delivered():
                continue
            order_state.delivered_at = int(time.time())
            ignored_pending += 1

        if self.initial_lookback_seconds > 0:
            self.state.last_seen_created_time = max(
                0,
                int(time.time()) - self.initial_lookback_seconds,
            )
            mode = "lookback window"
        else:
            self.state.last_seen_created_time = newest_created_time
            mode = "latest invoice tip"
        self.state.save()
        log(
            "Initialized invoice watermark from "
            f"{mode}; scanned {scanned} invoice(s), tracked {tracked_pending} unsettled "
            f"invoice(s), ignored {ignored_pending} pending delivery record(s)"
        )

    def _refresh_pending_invoices(self) -> None:
        pending_invoice_ids = list(self.state.pending_invoices.keys())
        for invoice_id in pending_invoice_ids:
            pending = self.state.pending_invoices.get(invoice_id)
            if pending is None:
                continue
            invoice = self.btcpay.get_invoice(invoice_id)
            if self._product_for_invoice(invoice) is None:
                self.state.pending_invoices.pop(invoice_id, None)
                self.state.save()
                continue
            if invoice_is_settled(invoice):
                product = self.products.get(pending.product_id)
                if product is None:
                    self.state.pending_invoices.pop(invoice_id, None)
                    self.state.save()
                    log(
                        "WARN: Ignoring settled pending invoice "
                        f"{invoice_id}; product {pending.product_id} is no longer configured"
                    )
                    continue
                self.process_order(
                    pending.order_id,
                    product=product,
                    subscriber_email=self._require_invoice_buyer_email(invoice),
                    invoice=invoice,
                )
                self.state.pending_invoices.pop(invoice_id, None)
                self.state.save()
                continue
            if invoice_is_terminal(invoice):
                self.state.pending_invoices.pop(invoice_id, None)
                self.state.save()

    def _remember_pending_invoice(
        self,
        invoice: dict[str, object],
        product: SubscriptionProduct,
    ) -> None:
        invoice_id = str(invoice.get("id") or "")
        order_id = invoice_order_id(invoice)
        if not invoice_id or not order_id:
            return
        if order_id in self.state.processed_orders:
            return
        product_id = self.product_ids_by_trial_pos_id[product.trial_pos_id]
        self.state.pending_invoices[invoice_id] = PendingInvoiceState(
            invoice_id=invoice_id,
            order_id=order_id,
            product_id=product_id,
            plan_id=product.plan_id,
            created_time=_invoice_created_time(invoice),
        )
        self.state.save()

    def _full_invoice(self, invoice: dict[str, object]) -> dict[str, object]:
        invoice_id = str(invoice.get("id") or "")
        if not invoice_id:
            return invoice
        return self.btcpay.get_invoice(invoice_id)

    def _product_for_invoice(
        self,
        invoice: dict[str, object],
    ) -> SubscriptionProduct | None:
        order_id = invoice_order_id(invoice)
        if not order_id or not is_secret_order_id(order_id):
            return None
        matched_products: dict[str, SubscriptionProduct] = {}
        for item_id in invoice_item_ids(invoice):
            product = self.products_by_trial_pos_id.get(item_id)
            if product is not None:
                matched_products[
                    self.product_ids_by_trial_pos_id[product.trial_pos_id]
                ] = product
        if len(matched_products) == 1:
            return next(iter(matched_products.values()))
        if len(matched_products) > 1:
            invoice_id = str(invoice.get("id") or "<unknown>")
            log(
                "WARN: Ignoring ambiguous invoice "
                f"{invoice_id} for order {order_id}; matched configured items "
                f"{', '.join(sorted(product.trial_pos_id for product in matched_products.values()))}"
            )
        return None

    def _require_invoice_buyer_email(self, invoice: dict[str, object]) -> str:
        subscriber_email = invoice_buyer_email(invoice)
        if subscriber_email is not None:
            return subscriber_email
        invoice_id = str(invoice.get("id") or "<unknown>")
        raise RuntimeError(f"Invoice {invoice_id} is missing metadata.buyerEmail")

    def _warn_on_product_price_mismatches(self) -> None:
        if not self.pos_base_url:
            log(
                "WARN: Cannot validate POS item prices because no pos_base_url is configured"
            )
            return
        try:
            app_id = pos_app_id_from_url(self.pos_base_url)
            pos_app = self.btcpay.get_pos_app(app_id)
        except Exception as exc:
            log(f"WARN: Could not load BTCPay POS app for price validation: {exc}")
            return

        pos_currency = str(pos_app.get("currency") or "")
        pos_items = {
            str(item.get("id")): item
            for item in pos_app.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

        for product_id, product in self.products.items():
            pos_item = pos_items.get(product.pos_id)
            if pos_item is None:
                log(
                    "WARN: Configured product "
                    f"{product_id} with POS item {product.pos_id} is missing "
                    "from the BTCPay POS app"
                )
                continue
            try:
                plan = self.btcpay.get_offering_plan(
                    offering_id=product.offering_id,
                    plan_id=product.plan_id,
                )
            except Exception as exc:
                log(
                    "WARN: Could not load BTCPay plan "
                    f"{product.plan_id} for product {product_id}: {exc}"
                )
                continue

            plan_currency = str(plan.get("currency") or "")
            if plan_currency and pos_currency and plan_currency != pos_currency:
                log(
                    "WARN: Currency mismatch for product "
                    f"{product_id} (POS item {product.pos_id}): "
                    f"POS={pos_currency} plan={plan_currency}"
                )
                continue

            item_price = parse_decimal(pos_item.get("price"))
            plan_price = parse_decimal(plan.get("price"))
            if item_price is None or plan_price is None:
                log(
                    "WARN: Could not parse prices for product "
                    f"{product_id} (POS item {product.pos_id}): "
                    f"item={pos_item.get('price')} plan={plan.get('price')}"
                )
                continue
            if item_price != plan_price:
                log(
                    "WARN: Price mismatch for product "
                    f"{product_id} (POS item {product.pos_id}): "
                    f"POS={item_price} {pos_currency or plan_currency} "
                    f"plan={plan_price} {plan_currency or pos_currency}"
                )

    def _wait_for_activated_checkout(self, checkout_id: str) -> dict[str, object]:
        deadline = time.time() + 20
        last_checkout: dict[str, object] | None = None
        while time.time() < deadline:
            checkout = self.btcpay.get_plan_checkout(checkout_id)
            last_checkout = checkout
            subscriber = checkout.get("subscriber")
            if checkout.get("planStarted") and isinstance(subscriber, dict):
                return checkout
            time.sleep(1)
        if last_checkout is None:
            raise RuntimeError(f"Checkout {checkout_id} did not become readable")
        raise RuntimeError(
            "Checkout did not return a subscriber after activation: "
            f"planStarted={last_checkout.get('planStarted')}"
        )

    def _find_existing_customer_selector(
        self,
        product: SubscriptionProduct,
        subscriber_email: str,
    ) -> str | None:
        for selector in (f"Email:{subscriber_email}", subscriber_email):
            subscriber = self.btcpay.get_subscriber(product.offering_id, selector)
            if not isinstance(subscriber, dict):
                continue

            customer = subscriber.get("customer")
            if not isinstance(customer, dict):
                continue
            customer_id = customer.get("id")

            if not self._subscriber_matches_product(subscriber, product):
                # Reusing the same subscriber across plans is intentional here.
                # BTCPay keeps a stable customer identity while the subscriber
                # may upgrade or downgrade between plans inside the offering.
                logger.info(
                    f"{subscriber_email=} requested {product=}. Returning the existing plan {subscriber.get('plan')=}."
                )

            if isinstance(customer_id, str) and customer_id:
                return customer_id
        return None

    def _can_reuse_existing_subscriber(
        self,
        subscriber_email: str,
        message_to_be_signed: str | None,
    ) -> bool:
        if not self.reuse_existing_subscriber_by_email:
            return False
        if not message_to_be_signed:
            return False
        return subscriber_email == derive_subscriber_email(message_to_be_signed)

    @staticmethod
    def _subscriber_matches_product(
        subscriber: dict[str, object],
        product: SubscriptionProduct,
    ) -> bool:
        plan = subscriber.get("plan")
        if not isinstance(plan, dict):
            return False
        plan_id = plan.get("id")
        return isinstance(plan_id, str) and plan_id == product.plan_id

    def _create_and_send_portal(
        self,
        order_id: str,
        product: SubscriptionProduct,
        customer_selector: str,
        checkout_id: str | None = None,
        origin_npub: str | None = None,
    ) -> str:
        portal = self.btcpay.create_portal_session(
            offering_id=product.offering_id,
            customer_selector=customer_selector,
            duration_minutes=self.portal_duration_minutes,
        )
        portal_url = str(portal["url"])
        product_id = self.product_ids_by_trial_pos_id[product.trial_pos_id]
        self.state.processed_orders[order_id] = ProcessedOrderState(
            product_id=product_id,
            plan_id=product.plan_id,
            customer_selector=customer_selector,
            portal_url=portal_url,
            origin_npub=origin_npub,
            checkout_id=checkout_id,
        )
        self.state.save()
        self._deliver_order(order_id, self.state.processed_orders[order_id])
        return portal_url

    def _deliver_pending_orders(self) -> None:
        for order_id, order_state in self.state.processed_orders.items():
            if order_state.is_delivered():
                continue
            try:
                self._deliver_order(order_id, order_state)
            except RuntimeError as exc:
                log(f"WARN: Pending delivery setup failed for {order_id}: {exc}")

    def _deliver_order(
        self,
        order_id: str,
        order_state: ProcessedOrderState,
    ) -> None:
        if order_state.is_delivered():
            return
        pending_future = self.delivery_futures.get(order_id)
        if pending_future is not None:
            self._finalize_delivery_future(order_id, order_state, pending_future)
            if order_id in self.delivery_futures or order_state.is_delivered():
                return
        if (
            self.max_delivery_attempts > 0
            and order_state.delivery_attempts >= self.max_delivery_attempts
        ):
            return
        recipient_npub = order_state.origin_npub
        if recipient_npub is None:
            log(f"WARN: Missing origin_npub for {order_id}; cannot deliver portal")
            return
        if not self.sender_nsec:
            raise RuntimeError(
                "Missing daemon.nsec_bitcoin_safe_pos for NIP-17 management replies"
            )
        log(f"Attempting NIP-17 delivery for {order_id}")
        order_state.delivery_attempts += 1
        order_state.last_delivery_attempt_at = int(time.time())
        self.state.save()
        payload = order_state.delivery_payload()
        self.delivery_futures[order_id] = self.transport.send_text_background(
            sender_nsec=self.sender_nsec,
            recipient_npub=recipient_npub,
            message=serialize_management_payload(payload),
            timeout_seconds=self.delivery_timeout_seconds,
            key=f"delivery:{order_id}",
        )

    def _finalize_delivery_future(
        self,
        order_id: str,
        order_state: ProcessedOrderState,
        delivery_future: Future[bool],
    ) -> None:
        if not delivery_future.done():
            return
        self.delivery_futures.pop(order_id, None)
        try:
            delivered = delivery_future.result()
        except RuntimeError as exc:
            log(f"WARN: NIP-17 delivery failed for {order_id}: {exc}")
            return
        except Exception as exc:
            log(f"WARN: Unexpected NIP-17 delivery failure for {order_id}: {exc}")
            return
        if not delivered:
            if (
                self.max_delivery_attempts > 0
                and order_state.delivery_attempts >= self.max_delivery_attempts
            ):
                log(
                    f"WARN: No relay accepted the NIP-17 message for {order_id}; "
                    f"stopping after {order_state.delivery_attempts} attempts"
                )
                return
            log(
                f"No relay accepted the NIP-17 message for {order_id}; will retry on the next poll"
            )
            return
        order_state.delivered_at = int(time.time())
        self.state.save()
        log(f"Delivered portal for {order_id}: {order_state.portal_url}")

    def _message_to_be_signed(
        self,
        invoice: dict[str, object] | None,
    ) -> str | None:
        if invoice is None:
            return None
        return invoice_message_to_be_signed(invoice)

    def _origin_npub_from_invoice(
        self, invoice: dict[str, object] | None
    ) -> str | None:
        if invoice is None:
            return None
        return invoice_origin_npub(invoice)


def payload_text_to_json(payload_text: str) -> object:
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Management payload is not valid JSON") from exc


def serialize_management_payload(payload: ManagementPayload) -> str:
    return json.dumps(payload.to_json(), separators=(",", ":"), sort_keys=True)
