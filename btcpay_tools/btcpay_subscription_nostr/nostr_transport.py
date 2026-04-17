from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from bitcoin_safe_lib.async_tools.loop_in_thread import LoopInThread, MultipleStrategy
from bitcoin_nostr_chat.default_relays import get_preferred_relays
from nostr_sdk import (
    Client,
    Event,
    Filter,
    HandleNotification,
    Keys,
    Kind,
    KindStandard,
    NostrSigner,
    PublicKey,
    RelayUrl,
    Timestamp,
    UnwrappedGift,
    uniffi_set_event_loop,
)


@dataclass(frozen=True)
class NostrIdentity:
    nsec: str
    npub: str


@dataclass(frozen=True)
class Nip17TransportConfig:
    relays: tuple[str, ...] = tuple(get_preferred_relays())
    lookback_seconds: int = 60
    subscription_id: str = "btcpay-subscription-management"
    replay_timeout_seconds: int = 5


class _NotificationHandler(HandleNotification):
    def __init__(
        self,
        signer: NostrSigner,
        expected_sender_npub: str,
        minimum_created_at: int,
        result_future: asyncio.Future[str],
    ) -> None:
        super().__init__()
        self._signer = signer
        self._expected_sender_npub = expected_sender_npub
        self._minimum_created_at = minimum_created_at
        self._result_future = result_future

    async def handle(
        self,
        relay_url: RelayUrl,
        subscription_id: str,
        event: Event,
    ) -> None:
        if (
            event.kind().as_std() != KindStandard.GIFT_WRAP
            or self._result_future.done()
        ):
            return
        try:
            unwrapped = await UnwrappedGift.from_gift_wrap(self._signer, event)
        except Exception:
            return
        sender_npub = unwrapped.sender().to_bech32()
        rumor = unwrapped.rumor()
        if sender_npub != self._expected_sender_npub:
            return
        if rumor.kind().as_std() != KindStandard.PRIVATE_DIRECT_MESSAGE:
            return
        if rumor.created_at().as_secs() < self._minimum_created_at:
            return
        if not self._result_future.done():
            self._result_future.set_result(rumor.content())

    async def handle_msg(self, relay_url: RelayUrl, msg: Any) -> None:
        return None


class Nip17Transport:
    _shared_lock = threading.Lock()
    _shared_loop_in_thread: LoopInThread | None = None
    _initialized_loops: set[int] = set()

    def __init__(
        self,
        config: Nip17TransportConfig | None = None,
        loop_in_thread: LoopInThread | None = None,
    ) -> None:
        self.config = config or Nip17TransportConfig()
        self.loop_in_thread = loop_in_thread or self._shared_loop()
        self._ensure_uniffi_loop(self.loop_in_thread)
        self._gift_wrap_kind = Kind.from_std(KindStandard.GIFT_WRAP)

    @classmethod
    def _shared_loop(cls) -> LoopInThread:
        if cls._shared_loop_in_thread is not None:
            return cls._shared_loop_in_thread
        with cls._shared_lock:
            if cls._shared_loop_in_thread is None:
                cls._shared_loop_in_thread = LoopInThread()
            return cls._shared_loop_in_thread

    @classmethod
    def _ensure_uniffi_loop(cls, loop_in_thread: LoopInThread) -> None:
        loop_id = id(loop_in_thread)
        if loop_id in cls._initialized_loops:
            return
        with cls._shared_lock:
            if loop_id in cls._initialized_loops:
                return
            loop_in_thread.run_foreground(cls._initialize_uniffi_loop())
            cls._initialized_loops.add(loop_id)

    @staticmethod
    async def _initialize_uniffi_loop() -> None:
        uniffi_set_event_loop(cast(asyncio.BaseEventLoop, asyncio.get_running_loop()))

    @staticmethod
    def generate_identity() -> NostrIdentity:
        keys = Keys.generate()
        return NostrIdentity(
            nsec=keys.secret_key().to_bech32(),
            npub=keys.public_key().to_bech32(),
        )

    def send_text_background(
        self,
        sender_nsec: str,
        recipient_npub: str,
        message: str,
        timeout_seconds: int,
        key: str | None = None,
    ) -> Future[bool]:
        strategy = (
            MultipleStrategy.REJECT_NEW_TASK
            if key is not None
            else MultipleStrategy.RUN_INDEPENDENT
        )
        return self.loop_in_thread.run_background(
            self._send_text(
                sender_nsec=sender_nsec,
                recipient_npub=recipient_npub,
                message=message,
                timeout_seconds=timeout_seconds,
            ),
            key=key,
            multiple_strategy=strategy,
        )

    async def _build_client(
        self,
        nsec: str,
    ) -> tuple[Client, Keys, NostrSigner, list[RelayUrl]]:
        keys = Keys.parse(nsec)
        signer = NostrSigner.keys(keys)
        client = Client(signer)
        relay_urls = self._relay_urls()
        for relay_url in relay_urls:
            await client.add_relay(relay_url)
        await client.connect()
        return client, keys, signer, relay_urls

    async def _send_text(
        self,
        sender_nsec: str,
        recipient_npub: str,
        message: str,
        timeout_seconds: int,
    ) -> bool:
        client, _, _, relay_urls = await self._build_client(sender_nsec)
        try:
            await client.wait_for_connection(timedelta(seconds=timeout_seconds))
            output = await asyncio.wait_for(
                client.send_private_msg_to(
                    relay_urls,
                    PublicKey.parse(recipient_npub),
                    message,
                ),
                timeout=timeout_seconds,
            )
            return bool(output.success)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Timed out sending a NIP-17 management message") from exc
        finally:
            await client.shutdown()

    def receive_text_background(
        self,
        receiver_nsec: str,
        expected_sender_npub: str,
        timeout_seconds: int,
        key: str | None = None,
    ) -> Future[str]:
        strategy = (
            MultipleStrategy.REJECT_NEW_TASK
            if key is not None
            else MultipleStrategy.RUN_INDEPENDENT
        )
        return self.loop_in_thread.run_background(
            self._receive_text(
                receiver_nsec=receiver_nsec,
                expected_sender_npub=expected_sender_npub,
                timeout_seconds=timeout_seconds,
            ),
            key=key,
            multiple_strategy=strategy,
        )

    async def _receive_text(
        self,
        receiver_nsec: str,
        expected_sender_npub: str,
        timeout_seconds: int,
    ) -> str:
        client, keys, signer, relay_urls = await self._build_client(receiver_nsec)
        started_at = Timestamp.now()
        result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        handler = _NotificationHandler(
            signer=signer,
            expected_sender_npub=expected_sender_npub,
            minimum_created_at=started_at.as_secs(),
            result_future=result_future,
        )
        notifications_task = asyncio.create_task(client.handle_notifications(handler))
        live_filter = (
            Filter().pubkey(keys.public_key()).kind(self._gift_wrap_kind).limit(0)
        )
        recent_filter = (
            Filter()
            .pubkey(keys.public_key())
            .kind(self._gift_wrap_kind)
            .since(
                Timestamp.from_secs(
                    max(0, started_at.as_secs() - self.config.lookback_seconds)
                )
            )
        )
        try:
            await client.wait_for_connection(timedelta(seconds=timeout_seconds))
            await client.subscribe_with_id(self.config.subscription_id, live_filter)
            await self._replay_recent_events(
                client=client,
                relay_urls=relay_urls,
                filter_=recent_filter,
                handler=handler,
            )
            return await asyncio.wait_for(result_future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "Timed out waiting for a NIP-17 management message"
            ) from exc
        finally:
            with suppress(Exception):
                await client.unsubscribe(self.config.subscription_id)
            with suppress(Exception):
                await client.shutdown()
            notifications_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await notifications_task

    async def _replay_recent_events(
        self,
        client: Client,
        relay_urls: list[RelayUrl],
        filter_: Filter,
        handler: _NotificationHandler,
    ) -> None:
        events = await client.fetch_events_from(
            relay_urls,
            filter_,
            timedelta(seconds=self.config.replay_timeout_seconds),
        )
        replay_relay = (
            relay_urls[0] if relay_urls else RelayUrl.parse(self.config.relays[0])
        )
        for event in events.to_vec():
            await handler.handle(replay_relay, self.config.subscription_id, event)

    def _relay_urls(self) -> list[RelayUrl]:
        return [RelayUrl.parse(url) for url in self.config.relays]
