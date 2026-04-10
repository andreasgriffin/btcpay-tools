from __future__ import annotations

import argparse
import asyncio
import sys
from concurrent.futures import Future
from pathlib import Path
from typing import TypeVar
from unittest.mock import Mock, patch

import pytest

from btcpay_tools.config import PlanDuration, SubscriptionProduct
from btcpay_tools.btcpay_subscription_nostr import daemon as daemon_module
from btcpay_tools.btcpay_subscription_nostr.daemon import (
    build_daemon,
    print_nostr_keypair,
)
from btcpay_tools.btcpay_subscription_nostr.management_payload import (
    ManagementPayload,
)
from btcpay_tools.btcpay_subscription_nostr.nostr_transport import (
    NostrIdentity,
)
from btcpay_tools.btcpay_subscription_nostr.core import (
    PendingInvoiceState,
    PosInvoiceMetadata,
    ProcessedOrderState,
    derive_subscriber_email,
)
from btcpay_tools.btcpay_subscription_nostr.service import (
    PurchaseSession,
    SubscriptionManagementClient,
    SubscriptionManagementPhase,
    SubscriptionManagementStatus,
    SubscriptionManagementStatusCode,
    SubscriptionDaemon,
    SubscriptionPurchaseClient,
    serialize_management_payload,
)

T = TypeVar("T")


def build_product(
    pos_id: str = "paid-pos",
    trial_pos_id: str | None = None,
    offering_id: str = "offering-paid",
    plan_id: str = "paid-plan",
    duration: PlanDuration = PlanDuration.MONTH,
) -> SubscriptionProduct:
    return SubscriptionProduct(
        pos_id=pos_id,
        trial_pos_id=trial_pos_id or pos_id,
        offering_id=offering_id,
        plan_id=plan_id,
        duration=duration,
    )


def test_parse_trial_status() -> None:
    html = """
    <h5 class="d-flex align-items-center gap-3 mb-2">
      <span data-testid="plan-name">Pro</span>
      <span class="badge badge-translucent rounded-pill text-bg-info">Trial</span>
    </h5>
    <span class="subscriber-status badge badge-translucent rounded-pill text-bg-success">
      <form class="dropdown">
        <a class="suspend-subscriber-link dropdown-item">Suspend Access</a>
      </form>
    </span>
    <input type="checkbox" id="autoRenewal" checked>
    """

    status = SubscriptionManagementClient.parse_management_page_status(html)

    assert status.status == SubscriptionManagementStatusCode.TRIAL
    assert status.phase == SubscriptionManagementPhase.TRIAL
    assert status.is_active is True
    assert status.auto_renew is True


def test_parse_suspended_status() -> None:
    html = """
    <h5>
      <span data-testid="plan-name">Pro</span>
    </h5>
    <span class="subscriber-status badge badge-translucent rounded-pill text-bg-danger">
      <form class="dropdown">
        <button type="submit" name="command" value="unsuspend">Unsuspend Access</button>
      </form>
    </span>
    """

    status = SubscriptionManagementClient.parse_management_page_status(html)

    assert status.status == SubscriptionManagementStatusCode.SUSPENDED
    assert status.phase == SubscriptionManagementPhase.NORMAL
    assert status.is_suspended is True
    assert status.is_active is False


def test_parse_payment_due_status() -> None:
    html = """
    <h5>
      <span data-testid="plan-name">Pro</span>
    </h5>
    <span class="subscriber-status badge badge-translucent rounded-pill text-bg-success"></span>
    <form method="post">
      <button type="submit" name="command" value="pay">Pay Now</button>
    </form>
    """

    status = SubscriptionManagementClient.parse_management_page_status(html)

    assert status.status == SubscriptionManagementStatusCode.PAYMENT_DUE
    assert status.phase == SubscriptionManagementPhase.NORMAL
    assert status.payment_due is True


def test_get_management_status_handles_not_found_page() -> None:
    client = SubscriptionManagementClient()
    response = Mock(status_code=404, text="not found")

    with patch.object(client.session, "get", return_value=response):
        status = client._get_management_status(
            "https://example.com/subscriber-portal/ps123"
        )

    assert status.status == SubscriptionManagementStatusCode.NOT_FOUND
    assert status.http_status == 404


def test_get_management_status_handles_not_found_page_async() -> None:
    client = SubscriptionManagementClient()
    response = Mock(status_code=404, text="not found")

    with patch.object(client.session, "get", return_value=response):
        status = asyncio.run(
            client.get_management_status("https://example.com/subscriber-portal/ps123")
        )

    assert status.status == SubscriptionManagementStatusCode.NOT_FOUND
    assert status.http_status == 404


def test_get_management_status_uses_loop_in_thread() -> None:
    response = Mock(status_code=404, text="not found")
    loop_in_thread = Mock()
    loop_in_thread.run_background.side_effect = lambda coro: (
        coro.close(),
        resolved_future(
            SubscriptionManagementStatus(
                status=SubscriptionManagementStatusCode.NOT_FOUND,
                phase=SubscriptionManagementPhase.UNKNOWN,
                is_active=None,
                is_suspended=False,
                http_status=404,
            )
        ),
    )[1]
    client = SubscriptionManagementClient(loop_in_thread=loop_in_thread)

    with patch.object(client.session, "get", return_value=response):
        status = asyncio.run(
            client.get_management_status("https://example.com/subscriber-portal/ps123")
        )

    assert status.status == SubscriptionManagementStatusCode.NOT_FOUND
    assert status.http_status == 404
    loop_in_thread.run_background.assert_called_once()


def test_management_client_close_stops_owned_loop_in_thread() -> None:
    client = SubscriptionManagementClient()
    client.loop_in_thread = Mock()

    client.close()

    client.loop_in_thread.stop.assert_called_once()


def test_management_client_close_does_not_stop_injected_loop_in_thread() -> None:
    loop_in_thread = Mock()
    client = SubscriptionManagementClient(loop_in_thread=loop_in_thread)

    client.close()

    loop_in_thread.stop.assert_not_called()


def build_purchase_client(
    metadata: PosInvoiceMetadata | None = None,
    npub_bitcoin_safe_pos: str = "npub1daemonexample",
    loop_in_thread: Mock | None = None,
    transport: Mock | None = None,
) -> tuple[SubscriptionPurchaseClient, Mock]:
    mocked_transport = transport or Mock()
    with patch(
        "btcpay_tools.btcpay_subscription_nostr.service.Nip17Transport",
        return_value=mocked_transport,
    ):
        client = SubscriptionPurchaseClient(
            pos_base_url="https://example.com/apps/pos/app123",
            pos_item_id="item123",
            metadata=metadata or PosInvoiceMetadata(buyer_email=""),
            npub_bitcoin_safe_pos=npub_bitcoin_safe_pos,
            loop_in_thread=loop_in_thread,
        )
    return client, mocked_transport


def test_start_purchase_generates_ephemeral_nostr_identity() -> None:
    transport = Mock()
    transport.generate_identity.return_value = NostrIdentity(
        nsec="nsec1session",
        npub="npub1session",
    )
    client, _ = build_purchase_client(
        npub_bitcoin_safe_pos="npub1example",
        transport=transport,
    )

    with (
        patch(
            "btcpay_tools.btcpay_subscription_nostr.service.generate_order_id",
            return_value="123456-deadbeefdeadbeef",
        ),
        patch(
            "btcpay_tools.btcpay_subscription_nostr.service.submit_pos_purchase",
            return_value="https://example.com/invoice/123",
        ) as mocked_submit,
    ):
        session = client._start_purchase()

    metadata = mocked_submit.call_args.kwargs["metadata"]
    assert session.origin_npub == "npub1session"
    assert session.receiver_nsec == "nsec1session"
    assert metadata.origin_npub == session.origin_npub
    assert metadata.buyer_email == "123456-deadbeefdeadbeef@v0.bitcoin-safe.org"


def test_start_purchase_uses_loop_in_thread() -> None:
    session = PurchaseSession(
        order_id="123456-deadbeefdeadbeef",
        receipt_url="https://example.com/invoice/123",
        origin_npub="npub1session",
        receiver_nsec="nsec1session",
    )
    loop_in_thread = Mock()
    loop_in_thread.run_background.side_effect = lambda coro: (
        coro.close(),
        resolved_future(session),
    )[1]
    with patch(
        "btcpay_tools.btcpay_subscription_nostr.service.Nip17Transport"
    ) as mocked_transport:
        client = SubscriptionPurchaseClient(
            pos_base_url="https://example.com/apps/pos/app123",
            pos_item_id="item123",
            metadata=PosInvoiceMetadata(buyer_email=""),
            npub_bitcoin_safe_pos="npub1example",
            loop_in_thread=loop_in_thread,
        )

    async_session = asyncio.run(client.start_purchase())

    assert async_session == session
    mocked_transport.assert_called_once_with(loop_in_thread=loop_in_thread)
    loop_in_thread.run_background.assert_called_once()


def test_close_stops_owned_loop_in_thread() -> None:
    client, _ = build_purchase_client()
    client.loop_in_thread = Mock()

    client.close()

    client.loop_in_thread.stop.assert_called_once()


def test_close_does_not_stop_injected_loop_in_thread() -> None:
    loop_in_thread = Mock()
    client, _ = build_purchase_client(
        loop_in_thread=loop_in_thread,
    )

    client.close()

    loop_in_thread.stop.assert_not_called()


def test_wait_for_management_payload_uses_session_identity() -> None:
    transport = Mock()
    transport.receive_text_background.return_value = resolved_future(
        serialize_management_payload(
            ManagementPayload(management_url="https://example.com/manage")
        )
    )
    client, _ = build_purchase_client(
        metadata=PosInvoiceMetadata(buyer_email="a@none.com"),
        npub_bitcoin_safe_pos="npub1daemonexample",
        transport=transport,
    )
    session = PurchaseSession(
        order_id="123456-deadbeefdeadbeef",
        receipt_url="https://example.com/invoice/123",
        origin_npub="npub1session",
        receiver_nsec="nsec1session",
    )

    payload = asyncio.run(client.wait_for_management_payload(session))

    assert payload.management_url == "https://example.com/manage"
    transport.receive_text_background.assert_called_once_with(
        receiver_nsec="nsec1session",
        expected_sender_npub="npub1daemonexample",
        timeout_seconds=client.timeout_seconds,
    )


def test_wait_for_management_payload_uses_session_identity_async() -> None:
    transport = Mock()
    transport.receive_text_background.return_value = resolved_future(
        serialize_management_payload(
            ManagementPayload(management_url="https://example.com/manage")
        )
    )
    client, _ = build_purchase_client(
        metadata=PosInvoiceMetadata(buyer_email="a@none.com"),
        npub_bitcoin_safe_pos="npub1daemonexample",
        transport=transport,
    )
    session = PurchaseSession(
        order_id="123456-deadbeefdeadbeef",
        receipt_url="https://example.com/invoice/123",
        origin_npub="npub1session",
        receiver_nsec="nsec1session",
    )

    payload = asyncio.run(client.wait_for_management_payload(session))

    assert payload.management_url == "https://example.com/manage"
    transport.receive_text_background.assert_called_once()


def test_start_and_wait_returns_management_payload() -> None:
    transport = Mock()
    transport.generate_identity.return_value = NostrIdentity(
        nsec="nsec1session",
        npub="npub1session",
    )
    transport.receive_text_background.return_value = resolved_future(
        serialize_management_payload(
            ManagementPayload(management_url="https://example.com/manage")
        )
    )
    client, _ = build_purchase_client(
        npub_bitcoin_safe_pos="npub1daemonexample",
        transport=transport,
    )

    with (
        patch(
            "btcpay_tools.btcpay_subscription_nostr.service.generate_order_id",
            return_value="123456-deadbeefdeadbeef",
        ),
        patch(
            "btcpay_tools.btcpay_subscription_nostr.service.submit_pos_purchase",
            return_value="https://example.com/invoice/123",
        ),
    ):
        session = asyncio.run(client.start_and_wait())

    assert session.management_payload is not None
    assert session.management_payload.management_url == "https://example.com/manage"


def resolved_future(value: T) -> Future[T]:
    future: Future[T] = Future()
    future.set_result(value)
    return future


def test_deliver_order_stops_after_max_attempts() -> None:
    state = Mock()
    transport = Mock()
    transport.send_text_background.side_effect = [
        resolved_future(False),
        resolved_future(False),
    ]
    order_state = ProcessedOrderState(
        product_id="paid",
        plan_id="paid-plan",
        customer_selector="customer-1",
        portal_url="https://example.com/portal",
        origin_npub="npub1recipient",
    )
    daemon = SubscriptionDaemon(
        btcpay=Mock(),
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=transport,
    )

    with patch("btcpay_tools.btcpay_subscription_nostr.service.log") as mocked_log:
        daemon._deliver_order("order-123", order_state)
        daemon._deliver_order("order-123", order_state)
        daemon._deliver_order("order-123", order_state)

    assert transport.send_text_background.call_count == 2
    assert order_state.delivery_attempts == 2
    logged_messages = [call.args[0] for call in mocked_log.call_args_list]
    assert any("stopping after 2 attempts" in message for message in logged_messages)


def test_deliver_order_sends_nip17_payload() -> None:
    state = Mock()
    transport = Mock()
    transport.send_text_background.return_value = resolved_future(True)
    order_state = ProcessedOrderState(
        product_id="paid",
        plan_id="paid-plan",
        customer_selector="customer-1",
        portal_url="https://example.com/portal",
        origin_npub="npub1recipient",
    )
    daemon = SubscriptionDaemon(
        btcpay=Mock(),
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=transport,
    )

    daemon._deliver_order("order-123", order_state)
    daemon._deliver_order("order-123", order_state)

    transport.send_text_background.assert_called_once_with(
        sender_nsec="nsec1sender",
        recipient_npub="npub1recipient",
        message=serialize_management_payload(order_state.delivery_payload()),
        timeout_seconds=5,
        key="delivery:order-123",
    )
    assert order_state.delivered_at is not None


def test_create_and_send_portal_persists_origin_npub() -> None:
    state = Mock()
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.create_portal_session.return_value = {"url": "https://example.com/portal"}
    transport = Mock()
    product = build_product()
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={"paid": product},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=transport,
    )

    with patch.object(daemon, "_deliver_order") as mocked_deliver:
        daemon._create_and_send_portal(
            order_id="order-123",
            product=product,
            customer_selector="customer-1",
            origin_npub="npub1recipient",
        )

    persisted_state = state.processed_orders["order-123"]
    assert persisted_state.origin_npub == "npub1recipient"
    assert persisted_state.product_id == "paid"
    mocked_deliver.assert_called_once()
    btcpay.create_portal_session.assert_called_once_with(
        offering_id="offering-paid",
        customer_selector="customer-1",
        duration_minutes=60,
    )


def test_process_order_reuses_existing_subscriber_by_email_when_enabled() -> None:
    state = Mock()
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.get_subscriber.return_value = {
        "customer": {"id": "customer-existing"},
        "plan": {"id": "paid-plan"},
    }
    transport = Mock()
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        reuse_existing_subscriber_by_email=True,
        sender_nsec="nsec1sender",
        transport=transport,
    )
    invoice: dict[str, object] = {
        "metadata": {
            "orderUrl": (
                "https://example.com/pos?"
                "orderUrl=https%3A%2F%2Fbtcpay-tools.invalid%2Fsubscription"
                "%3Fsigned_data0%3Dhello"
            )
        }
    }

    with patch.object(daemon, "_create_and_send_portal", return_value="portal-url"):
        portal_url = daemon.process_order(
            "order-123",
            product=build_product(),
            subscriber_email=derive_subscriber_email("hello"),
            invoice=invoice,
        )

    assert portal_url == "portal-url"
    btcpay.get_subscriber.assert_called_once_with(
        "offering-paid",
        f"Email:{derive_subscriber_email('hello')}",
    )


def test_process_order_reuses_existing_subscriber_with_wrong_plan() -> None:
    state = Mock()
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.get_subscriber.return_value = {
        "customer": {"id": "customer-existing"},
        "plan": {"id": "other-plan"},
    }
    btcpay.create_plan_checkout.return_value = {"id": "checkout-1"}
    transport = Mock()
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        reuse_existing_subscriber_by_email=True,
        sender_nsec="nsec1sender",
        transport=transport,
    )
    invoice: dict[str, object] = {
        "metadata": {
            "orderUrl": (
                "https://example.com/pos?"
                "orderUrl=https%3A%2F%2Fbtcpay-tools.invalid%2Fsubscription"
                "%3Fsigned_data0%3Dhello"
            )
        }
    }

    with patch.object(
        daemon, "_create_and_send_portal", return_value="portal-url"
    ) as mocked_portal:
        portal_url = daemon.process_order(
            "order-123",
            product=build_product(),
            subscriber_email=derive_subscriber_email("hello"),
            invoice=invoice,
        )

    assert portal_url == "portal-url"
    btcpay.create_plan_checkout.assert_not_called()
    mocked_portal.assert_called_once_with(
        "order-123",
        build_product(),
        "customer-existing",
        origin_npub=None,
    )


def test_process_order_uses_separate_offering_for_same_email() -> None:
    state = Mock()
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.get_subscriber.side_effect = [None, None]
    btcpay.create_plan_checkout.return_value = {"id": "checkout-1"}
    transport = Mock()
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        reuse_existing_subscriber_by_email=True,
        sender_nsec="nsec1sender",
        transport=transport,
    )
    invoice: dict[str, object] = {
        "metadata": {
            "orderUrl": (
                "https://example.com/pos?"
                "orderUrl=https%3A%2F%2Fbtcpay-tools.invalid%2Fsubscription"
                "%3Fsigned_data0%3Dhello"
            )
        }
    }

    with (
        patch.object(
            daemon,
            "_wait_for_activated_checkout",
            return_value={"subscriber": {"customer": {"id": "customer-activated"}}},
        ),
        patch.object(daemon, "_create_and_send_portal", return_value="portal-url"),
    ):
        daemon.process_order(
            "order-123",
            product=build_product(
                pos_id="business-pos",
                offering_id="offering-business",
                plan_id="business-plan",
            ),
            subscriber_email=derive_subscriber_email("hello"),
            invoice=invoice,
        )

    assert btcpay.get_subscriber.call_args_list == [
        (("offering-business", f"Email:{derive_subscriber_email('hello')}"),),
        (("offering-business", derive_subscriber_email("hello")),),
    ]
    btcpay.create_plan_checkout.assert_called_once_with(
        offering_id="offering-business",
        plan_id="business-plan",
        subscriber_email=derive_subscriber_email("hello"),
        is_trial=True,
    )


def test_process_order_uses_origin_npub_from_invoice() -> None:
    state = Mock()
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.create_plan_checkout.return_value = {"id": "checkout-1"}
    transport = Mock()
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=transport,
    )
    invoice: dict[str, object] = {
        "metadata": {
            "orderUrl": (
                "https://example.com/pos?"
                "orderUrl=https%3A%2F%2Fbtcpay-tools.invalid%2Fsubscription"
                "%3Forigin_npub%3Dnpub1recipient"
            )
        }
    }

    with (
        patch.object(
            daemon,
            "_wait_for_activated_checkout",
            return_value={"subscriber": {"customer": {"id": "customer-activated"}}},
        ),
        patch.object(
            daemon,
            "_create_and_send_portal",
            return_value="portal-url",
        ) as mocked_portal,
    ):
        portal_url = daemon.process_order(
            "123456-deadbeefdeadbeef",
            product=build_product(),
            subscriber_email="target@example.com",
            invoice=invoice,
        )

    assert portal_url == "portal-url"
    assert mocked_portal.call_args.kwargs["origin_npub"] == "npub1recipient"


def test_process_order_does_not_reuse_existing_subscriber_in_activation_fallback_without_validated_email() -> (
    None
):
    state = Mock()
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.create_plan_checkout.return_value = {"id": "checkout-1"}
    transport = Mock()
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        reuse_existing_subscriber_by_email=True,
        sender_nsec="nsec1sender",
        transport=transport,
    )

    with (
        patch.object(
            daemon,
            "_wait_for_activated_checkout",
            side_effect=RuntimeError("activation delayed"),
        ),
        patch.object(daemon, "_find_existing_customer_selector") as mocked_find,
    ):
        with pytest.raises(RuntimeError, match="activation delayed"):
            daemon.process_order(
                "order-123",
                product=build_product(),
                subscriber_email="forged@example.com",
                invoice={"metadata": {}},
            )

    mocked_find.assert_not_called()


def test_process_order_does_not_reuse_existing_subscriber_in_activation_fallback_when_disabled() -> (
    None
):
    state = Mock()
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.create_plan_checkout.return_value = {"id": "checkout-1"}
    transport = Mock()
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        reuse_existing_subscriber_by_email=False,
        sender_nsec="nsec1sender",
        transport=transport,
    )
    invoice: dict[str, object] = {
        "metadata": {
            "orderUrl": (
                "https://example.com/pos?"
                "orderUrl=https%3A%2F%2Fbtcpay-tools.invalid%2Fsubscription"
                "%3Fsigned_data0%3Dhello"
            )
        }
    }

    with (
        patch.object(
            daemon,
            "_wait_for_activated_checkout",
            side_effect=RuntimeError("activation delayed"),
        ),
        patch.object(daemon, "_find_existing_customer_selector") as mocked_find,
    ):
        with pytest.raises(RuntimeError, match="activation delayed"):
            daemon.process_order(
                "order-123",
                product=build_product(),
                subscriber_email=derive_subscriber_email("hello"),
                invoice=invoice,
            )

    mocked_find.assert_not_called()


def test_product_for_invoice_matches_pos_id() -> None:
    daemon = SubscriptionDaemon(
        btcpay=Mock(),
        state=Mock(),
        products={
            "paid": build_product(pos_id="paid-pos"),
        },
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=Mock(),
    )
    invoice: dict[str, object] = {
        "metadata": {
            "orderId": "123456-deadbeefdeadbeef",
            "itemCode": "paid-pos",
        }
    }

    product = daemon._product_for_invoice(invoice)

    assert product == build_product(pos_id="paid-pos")


def test_poll_once_processes_settled_invoice_at_watermark_boundary() -> None:
    invoice = {
        "id": "invoice123",
        "status": "Settled",
        "additionalStatus": "None",
        "createdTime": 100,
        "metadata": {
            "orderId": "123456-deadbeefdeadbeef",
            "itemCode": "trial-pos",
            "buyerEmail": "user@example.com",
        },
    }
    state = Mock()
    state.last_seen_created_time = 100
    state.pending_invoices = {}
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.list_invoices.return_value = [invoice]
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={
            "paid": build_product(pos_id="paid-pos", trial_pos_id="trial-pos"),
        },
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=Mock(),
    )
    daemon.startup_initialized = True
    daemon._full_invoice = Mock(return_value=invoice)
    daemon.process_order = Mock()
    daemon._refresh_pending_invoices = Mock()
    daemon._deliver_pending_orders = Mock()

    daemon.poll_once(page_size=50, max_pages=1)

    daemon.process_order.assert_called_once_with(
        "123456-deadbeefdeadbeef",
        product=build_product(pos_id="paid-pos", trial_pos_id="trial-pos"),
        subscriber_email="user@example.com",
        invoice=invoice,
    )


def test_deliver_pending_orders_logs_and_continues_when_sender_nsec_is_missing() -> (
    None
):
    state = Mock()
    state.processed_orders = {
        "order-1": ProcessedOrderState(
            product_id="paid",
            plan_id="paid-plan",
            customer_selector="customer-1",
            portal_url="https://example.com/portal",
            origin_npub="npub1recipient",
        )
    }
    daemon = SubscriptionDaemon(
        btcpay=Mock(),
        state=state,
        products={},
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec=None,
        transport=Mock(),
    )

    with patch("btcpay_tools.btcpay_subscription_nostr.service.log") as mocked_log:
        daemon._deliver_pending_orders()

    logged_messages = [call.args[0] for call in mocked_log.call_args_list]
    assert any(
        "Pending delivery setup failed for order-1" in message
        for message in logged_messages
    )


def test_poll_once_continues_processing_invoices_when_pending_delivery_setup_fails() -> (
    None
):
    pending_order = ProcessedOrderState(
        product_id="paid",
        plan_id="paid-plan",
        customer_selector="customer-pending",
        portal_url="https://example.com/pending-portal",
        origin_npub="npub1recipient",
    )
    invoice = {
        "id": "invoice123",
        "status": "Settled",
        "additionalStatus": "None",
        "createdTime": 100,
        "metadata": {
            "orderId": "123456-deadbeefdeadbeef",
            "itemCode": "trial-pos",
            "buyerEmail": "user@example.com",
        },
    }
    state = Mock()
    state.last_seen_created_time = 0
    state.pending_invoices = {}
    state.processed_orders = {"pending-order": pending_order}
    btcpay = Mock()
    btcpay.list_invoices.side_effect = [[invoice], []]
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={
            "paid": build_product(pos_id="paid-pos", trial_pos_id="trial-pos"),
        },
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec=None,
        transport=Mock(),
    )
    daemon.startup_initialized = True

    with (
        patch.object(daemon, "_refresh_pending_invoices"),
        patch.object(daemon, "_full_invoice", return_value=invoice),
        patch.object(
            daemon,
            "process_order",
            return_value="portal-url",
        ) as mocked_process,
    ):
        daemon.poll_once(page_size=10, max_pages=2)

    mocked_process.assert_called_once_with(
        "123456-deadbeefdeadbeef",
        product=build_product(pos_id="paid-pos", trial_pos_id="trial-pos"),
        subscriber_email="user@example.com",
        invoice=invoice,
    )


def test_refresh_pending_invoices_keeps_settled_invoice_when_process_order_fails() -> (
    None
):
    invoice_id = "invoice123"
    invoice = {
        "id": invoice_id,
        "status": "Settled",
        "additionalStatus": "None",
        "createdTime": 100,
        "metadata": {
            "orderId": "123456-deadbeefdeadbeef",
            "itemCode": "trial-pos",
            "buyerEmail": "user@example.com",
        },
    }
    state = Mock()
    state.pending_invoices = {
        invoice_id: PendingInvoiceState(
            invoice_id=invoice_id,
            order_id="123456-deadbeefdeadbeef",
            product_id="paid",
            plan_id="paid-plan",
            created_time=100,
        )
    }
    state.processed_orders = {}
    btcpay = Mock()
    btcpay.get_invoice.return_value = invoice
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=state,
        products={
            "paid": build_product(pos_id="paid-pos", trial_pos_id="trial-pos"),
        },
        pos_base_url=None,
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=Mock(),
    )
    daemon.process_order = Mock(side_effect=RuntimeError("temporary failure"))

    with pytest.raises(RuntimeError, match="temporary failure"):
        daemon._refresh_pending_invoices()

    assert invoice_id in state.pending_invoices
    state.save.assert_not_called()


def test_warn_on_product_price_mismatches_uses_paid_pos_item() -> None:
    btcpay = Mock()
    btcpay.get_pos_app.return_value = {
        "currency": "EUR",
        "items": [
            {"id": "paid-pos", "price": "10.00"},
            {"id": "trial-pos", "price": "0.00"},
        ],
    }
    btcpay.get_offering_plan.return_value = {"currency": "EUR", "price": "10.00"}
    daemon = SubscriptionDaemon(
        btcpay=btcpay,
        state=Mock(),
        products={
            "paid": build_product(pos_id="paid-pos", trial_pos_id="trial-pos"),
        },
        pos_base_url="https://example.com/apps/app123/pos",
        portal_duration_minutes=60,
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=60,
        sender_nsec="nsec1sender",
        transport=Mock(),
    )

    with patch("btcpay_tools.btcpay_subscription_nostr.service.log") as mocked_log:
        daemon._warn_on_product_price_mismatches()

    mocked_log.assert_not_called()


def test_build_daemon_derives_pos_base_url_from_base_url_and_pos_app_id(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
btcpay_base:
  base_url: https://example.com
  api_key: test-key
  pos_app_id: pos123
  store_id: store123
products:
  monthly:
    - offering_id: offering123
      plan_id: plan123
      pos_id: monthly-pos
      trial_pos_id: monthly-trial-pos
      duration: month
daemon:
  nsec_bitcoin_safe_pos: nsec1example
client:
  npub_bitcoin_safe_pos: npub1example
""".strip(),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=config_path,
        pos_base_url="",
        base_url="https://example.com",
        api_key="test-key",
        store_id="store123",
        http_timeout_seconds=20,
        portal_duration_minutes=60,
        command="run",
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=0,
        reuse_existing_subscriber_by_email=True,
        state_file=tmp_path / "daemon.state.json",
    )

    daemon = build_daemon(args)

    assert daemon.pos_base_url == "https://example.com/apps/pos123/pos"


def test_parse_args_generate_nostr_keypair_does_not_require_config(
    tmp_path: Path,
) -> None:
    with patch.object(
        sys,
        "argv",
        [
            "daemon",
            "--config",
            str(tmp_path / "missing.yaml"),
            "generate-nostr-keypair",
        ],
    ):
        args = daemon_module.parse_args()

    assert args.command == "generate-nostr-keypair"


def test_build_daemon_reuse_existing_subscriber_by_email_defaults_to_false(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
btcpay_base:
  base_url: https://example.com
  api_key: test-key
  pos_app_id: pos123
  store_id: store123
products:
  monthly:
    - offering_id: offering123
      plan_id: plan123
      pos_id: monthly-pos
      trial_pos_id: monthly-trial-pos
      duration: month
daemon:
  nsec_bitcoin_safe_pos: nsec1example
client:
  npub_bitcoin_safe_pos: npub1example
""".strip(),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=config_path,
        pos_base_url="",
        base_url="https://example.com",
        api_key="test-key",
        store_id="store123",
        http_timeout_seconds=20,
        portal_duration_minutes=60,
        command="run",
        delivery_timeout_seconds=5,
        max_delivery_attempts=2,
        initial_lookback_seconds=0,
        reuse_existing_subscriber_by_email=False,
        state_file=tmp_path / "daemon.state.json",
    )

    daemon = build_daemon(args)

    assert daemon.reuse_existing_subscriber_by_email is False


def test_print_nostr_keypair_includes_nsec_and_npub(capsys) -> None:
    print_nostr_keypair()

    output = capsys.readouterr().out
    assert "daemon.nsec_bitcoin_safe_pos=" in output
    assert "client.npub_bitcoin_safe_pos=" in output
    assert "YAML:" in output
