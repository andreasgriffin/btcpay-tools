from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from nostr_sdk import Keys, RelayUrl

from btcpay_tools.btcpay_subscription_nostr.nostr_transport import Nip17Transport


def test_send_text_times_out() -> None:
    transport = Nip17Transport()
    sender_keys = Keys.generate()
    recipient_keys = Keys.generate()
    client = AsyncMock()
    client.wait_for_connection = AsyncMock()
    client.send_private_msg_to = AsyncMock(side_effect=_sleep_forever)
    client.shutdown = AsyncMock()
    signer = Mock()
    relay_urls = [RelayUrl.parse("wss://relay.primal.net")]

    with patch.object(
        transport,
        "_build_client",
        AsyncMock(return_value=(client, sender_keys, signer, relay_urls)),
    ):
        with pytest.raises(
            RuntimeError,
            match="Timed out sending a NIP-17 management message",
        ):
            transport.send_text_background(
                sender_nsec=sender_keys.secret_key().to_bech32(),
                recipient_npub=recipient_keys.public_key().to_bech32(),
                message="hello",
                timeout_seconds=1,
            ).result(timeout=3)

    client.shutdown.assert_awaited_once()


def test_receive_text_times_out() -> None:
    transport = Nip17Transport()
    receiver_keys = Keys.generate()
    client = AsyncMock()
    client.wait_for_connection = AsyncMock()
    client.subscribe_with_id = AsyncMock()
    client.handle_notifications = AsyncMock(side_effect=_sleep_forever)
    client.unsubscribe = AsyncMock()
    client.shutdown = AsyncMock()
    signer = Mock()
    relay_urls = [RelayUrl.parse("wss://relay.primal.net")]

    with (
        patch.object(
            transport,
            "_build_client",
            AsyncMock(return_value=(client, receiver_keys, signer, relay_urls)),
        ),
        patch.object(transport, "_replay_recent_events", AsyncMock()),
    ):
        with pytest.raises(
            RuntimeError,
            match="Timed out waiting for a NIP-17 management message",
        ):
            transport.receive_text_background(
                receiver_nsec=receiver_keys.secret_key().to_bech32(),
                expected_sender_npub="npub1sender",
                timeout_seconds=1,
            ).result(timeout=3)

    client.wait_for_connection.assert_awaited_once()
    client.unsubscribe.assert_awaited_once()
    client.shutdown.assert_awaited_once()


async def _sleep_forever(*args: object, **kwargs: object) -> None:
    await asyncio.sleep(3600)
