"""Verify persistent reminders, acknowledgement from copies, and Telegram cleanup offline."""

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

from telegram import CallbackQuery, Chat, InputFile, Message, Update, User
from telegram.error import RetryAfter
lazy from telegram import InlineKeyboardMarkup

from grafana_telegram_relay.callbacks import Callbacks
from grafana_telegram_relay.delivery import Delivery, DeliveryOptions
from grafana_telegram_relay.models import acknowledgement_token
from grafana_telegram_relay.store import Store
from tests.factories import alert
from tests.fakes import Telegram, TelegramUpdates
lazy from grafana_telegram_relay.interfaces import ChatId
lazy from grafana_telegram_relay.models import Alert
lazy from tests.fakes import SentMessage


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    """Run timed deliveries against SQLite and a transport that records chat state."""

    @override
    async def asyncSetUp(self) -> None:
        """Create an isolated store and enable one-minute reminder intervals."""

        self.directory = tempfile.TemporaryDirectory()
        self.enterContext(patch.dict(os.environ, PTB_TIMEDELTA="true"))
        self.path = Path(self.directory.name) / "state.db"
        self.store = await Store.open(self.path, now=1000)
        self.telegram = Telegram()
        self.delivery = Delivery(self.store, self.telegram, "-123", None, DeliveryOptions(reminder_interval=60))
        self.item = alert()
        self.item.labels["mention"] = "@alice @bob"
        await self.store.ingest([self.item], now=1000)
        await self.delivery.once(now=1000)

    @override
    async def asyncTearDown(self) -> None:
        """Close SQLite and remove temporary data."""

        await self.store.close()
        self.directory.cleanup()

    async def drain(self, now: float) -> None:
        """Process bounded due work and fail if delivery never becomes idle."""

        for _ in range(20):
            if not await self.delivery.once(now=now):
                return

        self.fail("delivery did not become idle")

    async def acknowledge(self, message_id: int) -> str:
        """Apply an operator ACK using the incident token."""

        return await self.store.acknowledge(
            message_id, acknowledgement_token(self.item.key), user_id=42, name="@alice", now=1123
        )

    async def test_repeats_copy_formatted_message_without_requiring_more_webhooks(self) -> None:
        """Repeat only after the configured interval, keeping mentions and ACK controls."""

        assert not await self.delivery.once(now=1059)
        assert await self.delivery.once(now=1061)
        assert not await self.delivery.once(now=1100)
        assert await self.delivery.once(now=1122)

        assert set(self.telegram.messages) == {1, 2, 3}
        assert self.telegram.messages[2]["text"] == self.telegram.messages[1]["text"]
        assert self.telegram.messages[2]["parse_mode"] == "HTML"
        assert self.telegram.messages[2]["reply_markup"] == self.telegram.messages[1]["reply_markup"]
        assert "@alice @bob" in self.telegram.messages[3]["text"]

    async def test_callback_on_any_copy_acknowledges_original_and_deletes_all_copies(self) -> None:
        """Accept a real callback from a duplicate and retain only the acknowledged original."""

        await self.drain(now=1061)
        await self.drain(now=1122)
        markup = self.telegram.messages[3]["reply_markup"]
        assert markup is not None
        data = markup.inline_keyboard[0][0].callback_data
        assert isinstance(data, str)
        updates = TelegramUpdates()
        callbacks = Callbacks(self.store, updates, "-123")
        update = Update(
            123,
            callback_query=CallbackQuery(
                "copy-ack",
                User(42, "Alice", is_bot=False, username="alice"),
                "chat",
                message=Message(3, datetime.now(UTC), Chat(-123, "group")),
                data=data,
            ),
        )
        await callbacks.handle(update)
        await self.drain(now=1123)

        assert updates.answers == [("copy-ack", "Acknowledged")]
        assert set(self.telegram.messages) == {1}
        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]
        assert self.telegram.messages[1]["reply_markup"] is None
        assert not await self.delivery.once(now=2000)

    async def test_cleanup_survives_restart_and_disabled_reminders(self) -> None:
        """Keep failed deletions durable even after reminders are disabled."""

        await self.drain(now=1061)
        assert await self.acknowledge(1) == "acknowledged"
        self.telegram.fail_deletes = True
        await self.drain(now=1123)

        assert set(self.telegram.messages) == {1, 2}

        await self.store.close()
        self.store = await Store.open(self.path, now=1200)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        self.telegram.fail_deletes = False
        await self.drain(now=1200)

        assert set(self.telegram.messages) == {1}
        assert not await self.delivery.once(now=2000)

    async def test_resolution_stops_repeats_and_preserves_recovery_reply(self) -> None:
        """Clean unacknowledged copies on recovery without deleting the recovery reply."""

        await self.drain(now=1061)
        resolved = alert("resolved")
        resolved.labels["mention"] = "@alice @bob"
        await self.store.ingest([resolved], now=1062)
        await self.drain(now=1062)

        assert set(self.telegram.messages) == {1, 3}
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        assert self.telegram.messages[3]["text"] == "🟢 Решено\n@alice @bob"
        assert not await self.delivery.once(now=2000)

    async def test_copy_returning_after_ack_is_also_deleted(self) -> None:
        """Track an in-flight reminder even when another message is acknowledged first."""

        original = self.telegram.copy_message

        async def copy(
            *,
            chat_id: ChatId,
            from_chat_id: ChatId,
            message_id: int,
            reply_markup: InlineKeyboardMarkup | None,
        ) -> SentMessage:
            """Accept the ACK while Telegram is creating the duplicate."""

            assert await self.acknowledge(1) == "acknowledged"

            return await original(
                chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, reply_markup=reply_markup
            )

        with patch.object(self.telegram, "copy_message", copy):
            await self.delivery.once(now=1061)

        await self.drain(now=1123)

        assert set(self.telegram.messages) == {1}
        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]

    async def test_missing_copy_does_not_block_cleanup(self) -> None:
        """Forget an already deleted duplicate and keep processing the other copies."""

        await self.drain(now=1061)
        del self.telegram.messages[2]
        assert await self.acknowledge(1) == "acknowledged"
        await self.drain(now=1123)

        assert not await self.delivery.once(now=2000)
        assert set(self.telegram.messages) == {1}

    async def test_old_copies_are_removed_before_telegram_deletion_deadline(self) -> None:
        """Rotate day-old reminders while an incident is still unacknowledged."""

        await self.drain(now=1061)
        await self.drain(now=87500)

        assert set(self.telegram.messages) == {1, 3}
        assert await self.acknowledge(3) == "acknowledged"
        await self.drain(now=87501)

        assert set(self.telegram.messages) == {1}

    async def test_reminders_default_to_disabled(self) -> None:
        """Avoid new copies when the global repeat interval is zero."""

        self.delivery = Delivery(self.store, self.telegram, "-123", None)

        assert not await self.delivery.once(now=2000)
        assert set(self.telegram.messages) == {1}

    async def test_rate_limited_copy_respects_global_pause(self) -> None:
        """Pause both reminder and incident delivery after a Telegram rate limit."""

        with patch.object(self.telegram, "copy_message", side_effect=RetryAfter(30)):
            await self.delivery.once(now=1061)

        other = alert(starts_at="2026-10-17T13:00:00Z")
        await self.store.ingest([other], now=1062)

        assert not await self.delivery.once(now=1080)
        await self.drain(now=1092)
        assert len(self.telegram.messages) == 3

    async def test_deleted_original_is_recreated_before_repeating(self) -> None:
        """Recover a missing copy source instead of retrying the same absent message forever."""

        del self.telegram.messages[1]
        await self.drain(now=1061)

        assert set(self.telegram.messages) == {2}
        assert "FIRING" in self.telegram.messages[2]["text"]

        await self.drain(now=1122)

        assert set(self.telegram.messages) == {2, 3}
        assert await self.acknowledge(3) == "acknowledged"
        await self.drain(now=1123)

        assert set(self.telegram.messages) == {2}

    async def test_photo_is_copied_without_rendering_again(self) -> None:
        """Reuse the existing screenshot and its caption for reminder messages."""

        calls: list[str] = []

        async def render(item: Alert) -> bytes | None:
            """Record the screenshot requested for an actual alert update."""

            calls.append(item.status)

            return b"PNG"

        self.delivery.render = render
        self.item.values["A"] = 2
        await self.store.ingest([self.item], now=1001)
        await self.drain(now=1001)
        await self.drain(now=1061)

        assert calls == ["firing"]
        photo = self.telegram.messages[2]["photo"]
        assert isinstance(photo, InputFile)
        assert photo.input_file_content == b"PNG"
        assert self.telegram.messages[2]["caption"] == self.telegram.messages[1]["caption"]

    async def test_schedule_survives_restart_without_replaying_missed_intervals(self) -> None:
        """Resume one due repeat after downtime and schedule the next from its actual send."""

        await self.drain(now=1061)
        await self.store.close()
        self.store = await Store.open(self.path, now=1400)
        self.delivery = Delivery(self.store, self.telegram, "-123", None, DeliveryOptions(reminder_interval=60))
        await self.drain(now=1400)

        assert set(self.telegram.messages) == {1, 2, 3}
        assert not await self.delivery.once(now=1459)
        assert await self.acknowledge(2) == "acknowledged"
        await self.drain(now=1461)

        assert set(self.telegram.messages) == {1}

    async def test_retention_does_not_discard_failed_copy_deletions(self) -> None:
        """Finish cleanup even after resolved incident history has been pruned."""

        await self.drain(now=1061)
        self.telegram.fail_deletes = True
        await self.store.ingest([alert("resolved")], now=1062)
        await self.drain(now=1062)
        await self.store.cleanup(before=2000)
        self.telegram.fail_deletes = False
        await self.drain(now=1100)

        assert set(self.telegram.messages) == {1, 3}
        assert self.telegram.messages[3]["text"] == "🟢 Решено"
