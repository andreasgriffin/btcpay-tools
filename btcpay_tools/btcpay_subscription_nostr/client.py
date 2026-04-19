from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from btcpay_tools.config import (
    BTCPayBaseConfig,
    BTCPayConfig,
    PlanDuration,
    ProductCatalog,
    SubscriptionProduct,
)
from btcpay_tools.btcpay_subscription_nostr.core import (
    PosInvoiceMetadata,
)
from btcpay_tools.btcpay_subscription_nostr.service import (
    SubscriptionManagementClient,
    SubscriptionPurchaseClient,
)

import logging

logger = logging.getLogger(__file__)


def missing_products_config_error() -> RuntimeError:
    return RuntimeError(
        "Missing products config. Configure products.<product>[].offering_id, "
        "products.<product>[].plan_id, products.<product>[].pos_id, "
        "products.<product>[].trial_pos_id, and products.<product>[].duration entries."
    )


def parse_args() -> argparse.Namespace:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument(
        "--config", type=Path, default=BTCPayConfig.default_local_path()
    )
    bootstrap.add_argument("command", nargs="?")
    bootstrap_args, _ = bootstrap.parse_known_args()
    base_config: BTCPayBaseConfig | None = None
    product_catalog: ProductCatalog = {}
    default_product = ""
    subscriber_email = ""

    if bootstrap_args.command == "start":
        config = BTCPayConfig.load_file(bootstrap_args.config)
        base_config = config.btcpay_base
        product_catalog = config.products
        if not product_catalog:
            raise missing_products_config_error()
        subscriber_email = config.client.subscriber_email or ""
        default_product = config.client.default_product or next(iter(product_catalog))
        if default_product not in product_catalog:
            default_product = next(iter(product_catalog))

    parser = argparse.ArgumentParser(
        description=(
            "Create a BTCPay subscription purchase and wait for the subscriber "
            "portal URL over Nostr NIP-17."
        )
    )
    parser.add_argument(
        "--config", type=Path, default=BTCPayConfig.default_local_path()
    )
    parser.add_argument(
        "--base-url", default=base_config.base_url if base_config else ""
    )
    parser.add_argument(
        "--pos-app-id",
        default=base_config.pos_app_id if base_config else "",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--pos-base-url", default="")
    start.add_argument("--subscriber-email", default=subscriber_email)
    start.add_argument("--to-be-signed", default="")
    start.add_argument(
        "--product",
        default=default_product,
        choices=product_catalog,
    )
    start.add_argument(
        "--duration",
        choices=[duration.value for duration in PlanDuration],
        default=None,
    )
    start.add_argument("--open-management", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--management-url", required=True)

    return parser.parse_args()


def build_client(
    args: argparse.Namespace,
    product_catalog: ProductCatalog,
) -> SubscriptionPurchaseClient:
    config = BTCPayConfig.load_file(args.config)
    product = resolve_subscription_product(
        config,
        product_id=args.product,
        duration=args.duration,
    )
    pos_base_url = args.pos_base_url or None
    if pos_base_url is None:
        base_config = config.btcpay_base
        if not args.base_url:
            raise SystemExit(
                "Missing BTCPay base URL. Add btcpay.base_url to config or pass --base-url."
            )
        if not args.pos_app_id:
            raise SystemExit(
                "Missing BTCPay POS app ID. Add btcpay.pos_app_id to config or pass --pos-app-id."
            )
        pos_base_url = BTCPayBaseConfig(
            base_url=args.base_url,
            pos_app_id=args.pos_app_id,
            store_id=base_config.store_id,
        ).subscription_pos_base_url()
    return SubscriptionPurchaseClient(
        pos_base_url,
        product.trial_pos_id,
        PosInvoiceMetadata(
            buyer_email=args.subscriber_email or "",
            message_to_be_signed=args.to_be_signed or None,
            receipt_data=config.client.receipt_data,
        ),
        config.npub_bitcoin_safe_pos,
    )


def run_start(args: argparse.Namespace) -> int:
    async def run() -> int:
        config = BTCPayConfig.load_file(args.config)
        product_catalog = config.products
        if not product_catalog:
            raise missing_products_config_error()
        client = build_client(args, product_catalog)
        try:
            session = await client.start_purchase()
            print(f"order_id={session.order_id}")
            print(f"receipt_url={session.receipt_url}")
            management_payload = await client.wait_for_management_payload(
                session,
                open_management=args.open_management,
            )
            print(json.dumps(management_payload.to_json()))
            return 0
        finally:
            client.close()

    return asyncio.run(run())


def run_status(args: argparse.Namespace) -> int:
    async def run() -> int:
        client = SubscriptionManagementClient()
        try:
            status = await client.get_management_status(
                management_url=args.management_url
            )
            print(json.dumps(status.__dict__))
            return 0
        finally:
            client.close()

    return asyncio.run(run())


def main() -> int:
    args = parse_args()
    handlers = {"start": run_start, "status": run_status}
    return handlers[args.command](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


def resolve_subscription_product(
    config: BTCPayConfig,
    product_id: str,
    duration: str | None,
) -> SubscriptionProduct:
    try:
        return config.resolve_subscription(product_id, duration)
    except KeyError as exc:
        raise SystemExit(f"Unknown product {product_id!r}") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
