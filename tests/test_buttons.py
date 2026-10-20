"""Verify shared message buttons and durable acknowledgement through incident updates."""

import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest

from grafana_telegram_relay.delivery import Delivery
from grafana_telegram_relay.messages import MessageTemplates
from grafana_telegram_relay.store import Store
from tests.factories import alert
from tests.fakes import Telegram
lazy from grafana_telegram_relay.interfaces import ChatId
lazy from grafana_telegram_relay.models import Alert
lazy from tests.fakes import SentMessage


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    """Exercise acknowledgement against SQLite and recorded Telegram messages."""

    @override
    async def asyncSetUp(self) -> None:
        """Create an isolated store and delivery transport."""

        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.db"
        self.store = await Store.open(self.path, now=1000)
        self.telegram = Telegram()
        self.delivery = Delivery(self.store, self.telegram, "-123", None)

    @override
    async def asyncTearDown(self) -> None:
        """Close SQLite and remove temporary files."""

        await self.store.close()
        self.directory.cleanup()

    def keyboard(self) -> InlineKeyboardMarkup:
        """Return the inline keyboard recorded for the first incident."""

        markup = self.telegram.messages[1].get("reply_markup")
        assert isinstance(markup, InlineKeyboardMarkup)

        return markup

    def token(self) -> str:
        """Read the opaque acknowledgement token from the actual button."""

        data = self.keyboard().inline_keyboard[0][0].callback_data
        assert isinstance(data, str)
        assert data.startswith("ack:")
        assert len(data.encode()) <= 64

        return data.removeprefix("ack:")

    async def test_initial_keyboard_has_ack_in_its_own_row_and_optional_links(self) -> None:
        """Place ACK above Runbook, Grafana, and Silence without combining rows."""

        item = alert()
        item.annotations["runbook_url"] = "https://docs.example/runbook"
        item.dashboard_url = "https://grafana.example/d/infra/overview"
        item.silence_url = "https://grafana.example/alerting/silence/new?matcher=instance%3Dx"
        await self.store.ingest([item], now=1000)
        await self.delivery.once(now=1000)

        rows = self.keyboard().inline_keyboard
        assert [[button.text for button in row] for row in rows] == [["Acknowledge"], ["Runbook", "Grafana", "Silence"]]
        assert rows[1][0].url == item.annotations["runbook_url"]
        assert rows[1][1].url == item.dashboard_url
        assert rows[1][2].url == item.silence_url

    async def test_ack_persists_through_restart_updates_and_resolution(self) -> None:
        """Keep the first acknowledger until this incident resolves; reset on a new incident."""

        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        token = self.token()
        assert await self.store.acknowledge(1, token, user_id=42, name="@alice", now=1001) == "acknowledged"
        assert await self.store.acknowledge(1, token, user_id=43, name="@bob", now=1002) == "already_acknowledged"
        await self.delivery.once(now=1002)

        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]
        assert self.telegram.messages[1]["reply_markup"] is None
        assert len(self.telegram.messages) == 1

        await self.store.close()
        self.store = await Store.open(self.path, now=1003)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        item = alert()
        item.values["A"] = 2
        await self.store.ingest([item], now=1004)
        await self.delivery.once(now=1004)
        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]

        await self.store.ingest([alert("resolved")], now=1005)
        await self.delivery.once(now=1005)
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]

        await self.store.ingest([alert(starts_at="2026-10-17T13:00:00Z")], now=1006)
        await self.delivery.once(now=1006)
        assert "Acknowledged by" not in self.telegram.messages[3]["text"]
        markup = self.telegram.messages[3]["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup)
        assert markup.inline_keyboard[0][0].text == "Acknowledge"

    async def test_wrong_token_or_message_cannot_ack_another_incident(self) -> None:
        """Reject stale or fabricated callback identities without modifying the queue."""

        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        token = self.token()

        assert await self.store.acknowledge(1, "wrong", user_id=42, name="@alice", now=1001) == "not_found"
        assert await self.store.acknowledge(2, token, user_id=42, name="@alice", now=1001) == "not_found"
        assert await self.store.pending_count() == 0

        await self.store.ingest([alert("resolved")], now=1002)
        await self.delivery.once(now=1002)
        assert await self.store.acknowledge(1, token, user_id=42, name="@alice", now=1003) == "resolved"

    async def test_photo_ack_edits_caption_without_rendering_again(self) -> None:
        """Reserve caption space for acknowledgement and reuse the existing photo."""

        calls = []

        async def render(item: Alert) -> bytes | None:
            """Record screenshot requests and return an uploaded image."""

            calls.append(item.status)

            return b"PNG"

        root = Path(self.directory.name)
        (root / "long.html.j2").write_text("<b>{{ '🔥' * 1000 }}</b>")
        self.delivery.templates = MessageTemplates(root, default="long.html.j2")
        self.delivery.render = render
        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        token = self.token()
        await self.store.acknowledge(1, token, user_id=42, name="Alice <&>", now=1001)
        await self.delivery.once(now=1001)

        caption = self.telegram.messages[1]["caption"]
        assert len(caption.encode("utf-16-le")) // 2 <= 1024
        assert "Acknowledged by Alice <&>" in caption
        assert calls == ["firing"]
        assert self.telegram.messages[1]["photo"] == b"PNG"

    async def test_links_skip_invalid_urls_and_panel_fallback_opens_dashboard(self) -> None:
        """Exclude malformed links while preserving dashboard variables from a panel URL."""

        item = alert()
        item.annotations["runbook_url"] = "http://grafana:bad/runbook"
        item.silence_url = "http://:3000/alerting/silence/new"
        item.panel_url = "https://grafana.example/d/infra/overview?orgId=1&viewPanel=2&var-instance=x"
        await self.store.ingest([item], now=1000)
        await self.delivery.once(now=1000)

        rows = self.keyboard().inline_keyboard
        assert [[button.text for button in row] for row in rows] == [["Acknowledge"], ["Grafana"]]
        assert rows[1][0].url == "https://grafana.example/d/infra/overview?orgId=1&var-instance=x"

    async def test_ack_received_during_send_remains_pending(self) -> None:
        """Deliver an acknowledgement even if an older version is already being rendered."""

        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        token = self.token()
        item = alert()
        item.values["A"] = 2
        await self.store.ingest([item], now=1001)

        async def render(_item: Alert) -> bytes | None:
            """Acknowledge while the worker still owns the previous version snapshot."""

            await self.store.acknowledge(1, token, user_id=42, name="@alice", now=1002)

            return None

        self.delivery.render = render
        await self.delivery.once(now=1002)
        assert await self.store.pending_count() == 1
        await self.delivery.once(now=1003)
        assert "Acknowledged by @alice" in self.telegram.messages[1]["text"]
        assert await self.store.pending_count() == 0

    async def test_localhost_navigation_uses_numeric_loopback(self) -> None:
        """Use loopback IP URLs because Telegram rejects localhost in inline buttons."""

        item = alert()
        item.annotations["runbook_url"] = "http://localhost:8000/runbook"
        item.dashboard_url = "http://LOCALHOST:3000/grafana/d/infra/overview?orgId=1"
        item.silence_url = "http://localhost:3000/grafana/alerting/silence/new"
        await self.store.ingest([item], now=1000)
        await self.delivery.once(now=1000)

        links = self.keyboard().inline_keyboard[1]
        assert [button.url for button in links] == [
            "http://127.0.0.1:8000/runbook",
            "http://127.0.0.1:3000/grafana/d/infra/overview?orgId=1",
            "http://127.0.0.1:3000/grafana/alerting/silence/new",
        ]

    async def test_ack_does_not_skip_undelivered_payload_render(self) -> None:
        """Render new alert data when ACK is queued before its previous update was sent."""

        calls: list[object] = []

        async def render(item: Alert) -> bytes | None:
            """Record which query result was rendered for each delivered snapshot."""

            calls.append(item.values["A"])

            return b"PNG"

        self.delivery.render = render
        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        token = self.token()
        item = alert()
        item.values["A"] = 2
        await self.store.ingest([item], now=1001)
        await self.store.acknowledge(1, token, user_id=42, name="@alice", now=1002)
        await self.delivery.once(now=1002)

        assert calls == [1, 2]
        assert "Acknowledged by @alice" in self.telegram.messages[1]["caption"]

    async def test_rejected_url_buttons_do_not_block_incident_delivery(self) -> None:
        """Keep ACK and alert text if Telegram rejects an optional navigation URL."""

        original = self.telegram.send_message

        async def send(
            *,
            chat_id: ChatId,
            text: str,
            parse_mode: str | None,
            reply_markup: InlineKeyboardMarkup | None = None,
        ) -> SentMessage:
            """Reject navigation URLs as the real Bot API can, accepting callback buttons."""

            if reply_markup and any(button.url for row in reply_markup.inline_keyboard for button in row):
                msg = "Button_url_invalid"
                raise BadRequest(msg)

            return await original(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

        item = alert()
        item.annotations["runbook_url"] = "https://docs.example/runbook"
        await self.store.ingest([item], now=1000)

        with patch.object(self.telegram, "send_message", send):
            await self.delivery.once(now=1000)

        assert await self.store.pending_count() == 0
        assert [[button.text for button in row] for row in self.keyboard().inline_keyboard] == [["Acknowledge"]]
