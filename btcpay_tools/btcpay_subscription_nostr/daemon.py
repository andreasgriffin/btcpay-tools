from __future__ import annotations

import argparse
import os
from pathlib import Path

from btcpay_tools.config import (
    BTCPayBaseConfig,
    BTCPayConfig,
    DaemonConfig,
    PlanDuration,
    SubscriptionProduct,
)
from btcpay_tools.btcpay_subscription_nostr.core import (
    BtcpayClient,
    BtcpayConfig,
    PersistentState,
)
from btcpay_tools.btcpay_subscription_nostr.service import (
    SubscriptionDaemon,
)
from nostr_sdk import Keys


def _require_daemon_config(config: BTCPayConfig) -> DaemonConfig:
    daemon_config = config.daemon
    assert daemon_config is not None, (
        "Missing daemon config. Add a daemon section to the config file "
        "before running the daemon."
    )
    return daemon_config


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
    daemon_config: DaemonConfig | None = None
    product_catalog: dict[str, list[SubscriptionProduct]] | None = None

    if bootstrap_args.command in {"run", "process-order"}:
        config = BTCPayConfig.load_file(bootstrap_args.config)
        daemon_config = _require_daemon_config(config)
        base_config = config.btcpay_base
        product_catalog = config.products

    parser = argparse.ArgumentParser(
        description=(
            "Poll BTCPay invoices, create subscriptions for invoices that become "
            "settled, and deliver subscriber portals over Nostr NIP-17."
        )
    )
    parser.add_argument(
        "--config", type=Path, default=BTCPayConfig.default_local_path()
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("BTCPAY_API_KEY")
        or (base_config.api_key if base_config else "")
        or "",
    )
    parser.add_argument(
        "--base-url", default=base_config.base_url if base_config else ""
    )
    parser.add_argument(
        "--store-id",
        default=base_config.store_id if base_config else "",
    )
    parser.add_argument("--pos-base-url", default="")
    parser.add_argument(
        "--http-timeout-seconds",
        type=int,
        default=daemon_config.http_timeout_seconds if daemon_config else 20,
    )
    parser.add_argument(
        "--portal-duration-minutes",
        type=int,
        default=daemon_config.portal_duration_minutes if daemon_config else 1440,
    )
    parser.add_argument(
        "--reuse-existing-subscriber-by-email",
        action=argparse.BooleanOptionalAction,
        default=(
            daemon_config.reuse_existing_subscriber_by_email if daemon_config else False
        ),
        help=(
            "Reuse an existing BTCPay subscriber by email before creating a new "
            "checkout. Safe only when buyerEmail is a non-guessable email derived "
            "by the server, not chosen by the user. A forged buyerEmail can "
            "otherwise claim another subscriber's plan."
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=(
            daemon_config.state_file
            if daemon_config
            else Path("btcpay_tools/btcpay_subscription_nostr/daemon.state.json")
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_forever = subparsers.add_parser("run")
    run_forever.add_argument(
        "--poll-seconds",
        type=int,
        default=daemon_config.poll_seconds if daemon_config else 10,
    )
    run_forever.add_argument("--page-size", type=int, default=50)
    run_forever.add_argument(
        "--max-pages",
        type=int,
        default=daemon_config.max_pages if daemon_config else 5,
    )
    run_forever.add_argument(
        "--initial-lookback-seconds",
        type=int,
        default=daemon_config.initial_lookback_seconds if daemon_config else 0,
    )
    run_forever.add_argument(
        "--delivery-timeout-seconds",
        type=int,
        default=daemon_config.delivery_timeout_seconds if daemon_config else 5,
    )
    run_forever.add_argument(
        "--max-delivery-attempts",
        type=int,
        default=daemon_config.max_delivery_attempts if daemon_config else 10,
    )

    process_order = subparsers.add_parser("process-order")
    process_order.add_argument("--order-id", required=True)
    process_order.add_argument("--product", choices=product_catalog)
    process_order.add_argument(
        "--duration",
        choices=[duration.value for duration in PlanDuration],
        default=None,
    )
    process_order.add_argument("--subscriber-email", default="")

    subparsers.add_parser(
        "generate-nostr-keypair",
        description="Generate a Nostr keypair for NIP-17 management replies.",
    )

    return parser.parse_args()


def require_argument(value: str, message: str) -> str:
    if value:
        return value
    raise SystemExit(message)


def build_btcpay_client(args: argparse.Namespace) -> BtcpayClient:
    api_key = require_argument(
        args.api_key,
        "Missing API key. Set BTCPAY_API_KEY, add btcpay.api_key to config, or pass --api-key.",
    )
    base_url = require_argument(
        args.base_url,
        "Missing BTCPay base URL. Add btcpay.base_url to config or pass --base-url.",
    )
    store_id = require_argument(
        args.store_id,
        "Missing BTCPay store ID. Add btcpay.store_id to config or pass --store-id.",
    )
    config = BtcpayConfig(
        base_url=base_url,
        store_id=store_id,
        api_key=api_key,
        timeout_seconds=args.http_timeout_seconds,
    )
    return BtcpayClient(config)


def build_daemon(args: argparse.Namespace) -> SubscriptionDaemon:
    config = BTCPayConfig.load_file(args.config)
    daemon_config = _require_daemon_config(config)
    product_catalog = config.products
    if not product_catalog:
        raise missing_products_config_error()
    pos_base_url = args.pos_base_url or None
    if pos_base_url is None:
        base_config = config.btcpay_base
        pos_app_id = base_config.pos_app_id if base_config else None
        if args.base_url and pos_app_id:
            pos_base_url = BTCPayBaseConfig(
                base_url=args.base_url,
                pos_app_id=pos_app_id,
                store_id=base_config.store_id,
            ).subscription_pos_base_url()
    nsec_bitcoin_safe_pos = daemon_config.nsec_bitcoin_safe_pos
    state = PersistentState(args.state_file)
    state.load()
    delivery_timeout_seconds = 5
    max_delivery_attempts = 10
    initial_lookback_seconds = 0
    if args.command == "run":
        delivery_timeout_seconds = args.delivery_timeout_seconds
        max_delivery_attempts = args.max_delivery_attempts
        initial_lookback_seconds = args.initial_lookback_seconds
    return SubscriptionDaemon(
        btcpay=build_btcpay_client(args),
        state=state,
        products=config.subscription_products(),
        pos_base_url=pos_base_url,
        portal_duration_minutes=args.portal_duration_minutes,
        delivery_timeout_seconds=delivery_timeout_seconds,
        max_delivery_attempts=max_delivery_attempts,
        initial_lookback_seconds=initial_lookback_seconds,
        reuse_existing_subscriber_by_email=args.reuse_existing_subscriber_by_email,
        sender_nsec=nsec_bitcoin_safe_pos,
    )


def resolve_process_order_product(args: argparse.Namespace) -> SubscriptionProduct:
    config = BTCPayConfig.load_file(args.config)
    product_catalog = config.products
    if not product_catalog:
        raise missing_products_config_error()
    if args.product:
        try:
            return config.resolve_subscription(args.product, args.duration)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if len(product_catalog) == 1:
        product_id = next(iter(product_catalog))
        try:
            return config.resolve_subscription(product_id, args.duration)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    raise SystemExit(
        "Multiple products are configured. Pass --product <product-id> to process-order."
    )


def print_nostr_keypair() -> None:
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    npub = keys.public_key().to_bech32()
    print(f"daemon.nsec_bitcoin_safe_pos={nsec}")
    print(f"client.npub_bitcoin_safe_pos={npub}")
    print()
    print("YAML:")
    print("daemon:")
    print(f"  nsec_bitcoin_safe_pos: {nsec}")
    print("client:")
    print(f"  npub_bitcoin_safe_pos: {npub}")


def main() -> int:
    args = parse_args()
    if args.command == "generate-nostr-keypair":
        print_nostr_keypair()
        return 0
    daemon = build_daemon(args)
    if args.command == "process-order":
        product_catalog = BTCPayConfig.load_file(args.config).products
        if not product_catalog:
            raise missing_products_config_error()
        product = resolve_process_order_product(args)
        subscriber_email = require_argument(
            args.subscriber_email,
            "Missing subscriber email. Pass --subscriber-email for manual process-order.",
        )
        daemon.process_order(
            args.order_id,
            product=product,
            subscriber_email=subscriber_email,
        )
        return 0
    daemon.run_forever(
        poll_seconds=args.poll_seconds,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
