"""Verify Telegram callbacks, operator identity, and durable polling offsets offline."""

import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from grafana_telegram_relay.callbacks import Callbacks
from grafana_telegram_relay.delivery import Delivery
from grafana_telegram_relay.store import Store
from tests.factories import alert
from tests.fakes import Telegram, TelegramUpdates


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    """Exercise callbacks against an actual SQLite incident and fake Telegram updates."""

    @override
    async def asyncSetUp(self) -> None:
        """Deliver an incident whose actual callback data can be clicked by test users."""

        self.enterContext(patch.dict(os.environ, PTB_TIMEDELTA="true"))
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.db"
        self.store = await Store.open(self.path, now=1000)
        self.telegram = Telegram()
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        markup = self.telegram.messages[1]["reply_markup"]
        assert markup is not None
        data = markup.inline_keyboard[0][0].callback_data
        assert isinstance(data, str)
        self.data = data
        self.updates = TelegramUpdates()
        self.callbacks = Callbacks(self.store, self.updates, "-123")

    @override
    async def asyncTearDown(self) -> None:
        """Close the database and remove temporary files."""

        await self.store.close()
        self.directory.cleanup()

    def update(self, *, chat_id: int = -123, username: str | None = "alice", data: str | None = None) -> Update:
        """Create a real Telegram callback object for the delivered message."""

        return Update(
            123,
            callback_query=CallbackQuery(
                "callback-123",
                User(id=42, first_name="Alice <&>", is_bot=False, username=username),
                "chat-instance",
                message=Message(1, datetime.now(UTC), Chat(chat_id, "group")),
                data=self.data if data is None else data,
            ),
        )

    async def test_callback_acknowledges_and_repeated_click_is_idempotent(self) -> None:
        """Append the operator to the shared message while answering only the clicker."""

        update = self.update()
        await self.callbacks.handle(update)
        await self.delivery.once(now=1001)

        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]
        assert self.updates.answers == [("callback-123", "Acknowledged")]
        await self.callbacks.handle(update)
        assert self.updates.answers[-1] == ("callback-123", "Already acknowledged")
        assert await self.store.pending_count() == 0

    async def test_missing_username_uses_operator_display_name(self) -> None:
        """Identify operators who have no public Telegram username."""

        await self.callbacks.handle(self.update(username=None))
        await self.delivery.once(now=1001)

        assert "Acknowledged by Alice &lt;&amp;&gt;" in self.telegram.messages[1]["text"]

    async def test_other_chats_and_wrong_callback_data_cannot_acknowledge(self) -> None:
        """Reject forwarded or fabricated button identities without touching the incident."""

        await self.callbacks.handle(self.update(chat_id=-456))
        await self.callbacks.handle(self.update(data="ack:wrong-token"))
        await self.callbacks.handle(self.update(data="other:action"))

        assert await self.store.pending_count() == 0
        assert all(text == "Alert not found" for _, text in self.updates.answers)

    async def test_poll_offset_survives_restart_and_expired_callback(self) -> None:
        """Confirm consumed updates after durable ACK even if its popup expired."""

        self.updates.expired = True
        self.updates.batches.append((self.update(),))
        await self.callbacks.poll()
        assert await self.store.telegram_offset() == 124
        await self.delivery.once(now=1001)
        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]

        await self.store.close()
        self.store = await Store.open(self.path, now=1002)
        assert await self.store.telegram_offset() == 124

    async def test_poll_worker_recovers_from_rate_limit_and_can_be_cancelled(self) -> None:
        """Keep polling after Telegram backpressure without swallowing task cancellation."""

        self.updates.rate_limit = True
        self.updates.batches.append((self.update(),))
        task = asyncio.create_task(self.callbacks.run())

        try:
            await asyncio.wait_for(self.updates.waiting.wait(), timeout=1)
            assert await self.store.telegram_offset() == 124
            assert self.updates.requests[-1] == (124, 30, ("callback_query",))

        finally:
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task
