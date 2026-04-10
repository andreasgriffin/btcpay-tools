# BTCPay Subscription Nostr

The following API key permissions need to be set:
- btcpay.store.canviewinvoices
- btcpay.store.canviewofferings
- btcpay.store.canmanagesubscribers


Create a local config file named `btcpay_subscription_nostr.local.yaml`. The CLI reads that file by default, so you can run everything without passing individual flags.

The client config must include `client.subscriber_email`, which is submitted to the BTCPay POS as `buyerEmail`, for example:

```yaml
client:
  subscriber_email: andreasgriffin@proton.me
```

The daemon enables `daemon.reuse_existing_subscriber_by_email` by default. This is only safe when `buyerEmail` is a non-guessable, non-user-determined value that your server derives itself, such as `derive_subscriber_email(...)`. If users can choose or predict the email, a forged `buyerEmail` can reuse someone else's existing BTCPay subscriber and expose that subscriber's plan.

If that safety condition does not hold, disable it explicitly:

```yaml
daemon:
  reuse_existing_subscriber_by_email: false
```

Run the daemon:

```bash
poetry run python -m btcpay_tools.btcpay_subscription_nostr.daemon run
```

Start a purchase from the client:

```bash
poetry run python -m btcpay_tools.btcpay_subscription_nostr.client start
```

The client generates a fresh ephemeral Nostr keypair for each `start` command, includes only the `origin_npub` in the POS metadata, waits for the daemon reply on that key, and forgets the key when the process exits.

Management replies are delivered over Nostr NIP-17. Configure the daemon's reply key once and give the matching public key to the client:

```yaml
client:
  npub_bitcoin_safe_pos: <daemon-npub>

daemon:
  nsec_bitcoin_safe_pos: <daemon-nsec>
```

If you use `--to-be-signed` or `PosInvoiceMetadata.message_to_be_signed`, the tool still embeds that value into the BTCPay `orderUrl` metadata field under the query key `signed_data0`. The client now authenticates the management reply by accepting only NIP-17 messages unwrapped from `client.npub_bitcoin_safe_pos`.

Check the status of an existing subscription management URL:

```bash
poetry run python -m btcpay_tools.btcpay_subscription_nostr.client status --management-url <management-url>
```

This prints JSON, for example:

```json
{"status": "active", "phase": "normal", "is_active": true, "is_suspended": false, "pending_invoice": false, "payment_due": false, "upgrade_required": false, "auto_renew": true}
```

If your YAML file is in a different location, pass it explicitly:

```bash
poetry run python -m btcpay_tools.btcpay_subscription_nostr.daemon --config /path/to/config.yaml run
poetry run python -m btcpay_tools.btcpay_subscription_nostr.client --config /path/to/config.yaml start
```

You can generate the daemon/client Nostr keypair before writing a full daemon config:

```bash
poetry run python -m btcpay_tools.btcpay_subscription_nostr.daemon generate-nostr-keypair
```
