"""Verify persistent incident identity, Telegram edits, retries, and watchdog recovery."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import NoReturn, override
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY
from telegram.error import BadRequest, RetryAfter
lazy from telegram import InlineKeyboardMarkup

from grafana_telegram_relay.delivery import Delivery
from grafana_telegram_relay.messages import MessageTemplates
from grafana_telegram_relay.store import Store
from tests.factories import alert, alert_payload
from tests.fakes import Telegram
lazy from grafana_telegram_relay.interfaces import ChatId
lazy from grafana_telegram_relay.models import Alert
lazy from tests.fakes import SentMessage


class IncidentTests(unittest.IsolatedAsyncioTestCase):
    """Exercise delivery against a real SQLite queue and an in-memory Telegram chat."""

    @override
    async def asyncSetUp(self) -> None:
        """Create an isolated database and transport for each test."""

        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.db"
        self.store = await Store.open(self.path, now=1000)
        self.telegram = Telegram()
        self.delivery = Delivery(self.store, self.telegram, "-123", None)

    @override
    async def asyncTearDown(self) -> None:
        """Close asynchronous resources and remove temporary data."""

        await self.store.close()
        self.directory.cleanup()

    async def test_one_message_through_resolution_and_new_message_for_next_incident(self) -> None:
        """Verify one message through resolution and new message for next incident."""

        metric = "telegram_bot_delivery_duration_seconds_count"
        before = REGISTRY.get_sample_value(metric, {"result": "success"}) or 0
        firing = alert()
        firing.labels["mention"] = "@alice @bob"
        resolved = alert("resolved")
        resolved.labels["mention"] = "@alice @bob"

        await self.store.ingest([firing], now=1000)
        await self.delivery.once(now=1000)

        assert self.telegram.messages[1]["parse_mode"] == "HTML"
        assert self.telegram.messages[1]["text"].splitlines()[1] == "@alice @bob"

        await self.store.ingest([firing], now=1001)
        assert not await self.delivery.once(now=1001)

        await self.store.ingest([resolved], now=1002)
        await self.delivery.once(now=1002)

        assert len(self.telegram.messages) == 2
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        assert self.telegram.messages[1]["parse_mode"] == "HTML"
        assert self.telegram.messages[1]["text"].splitlines()[1] == "@alice @bob"
        reply = self.telegram.messages[2]
        assert reply["text"] == "🟢 Решено\n@alice @bob"
        assert reply["parse_mode"] is None
        assert reply["reply_markup"] is None
        parameters = reply["reply_parameters"]
        assert parameters is not None
        assert parameters.message_id == 1

        await self.store.ingest([alert(starts_at="2026-10-17T13:00:00Z")], now=1003)
        await self.delivery.once(now=1003)
        assert len(self.telegram.messages) == 3
        after = REGISTRY.get_sample_value(metric, {"result": "success"})
        assert after is not None
        assert after - before == 3

    async def test_persisted_payload_keeps_grafana_field_names_and_query_results(self) -> None:
        """Persist webhook data in the JSON shape consumed by delivery and SQL queries."""

        item = alert_payload("resolved", values={"A": [True, 2, 1.5, None, {"result": "ok"}]})
        await self.store.ingest([item], now=1000)
        row = await self.store.pending(now=1000)
        assert row is not None
        snapshot: dict[str, object] = json.loads(row["payload"])

        assert snapshot["values"] == {"A": [True, 2, 1.5, None, {"result": "ok"}]}
        assert snapshot["startsAt"] == "2026-10-17T12:00:00+00:00"
        assert snapshot["endsAt"] == "2026-10-17T12:10:00+00:00"
        assert "starts_at" not in snapshot
        assert "panelURL" in snapshot
        assert "panel_url" not in snapshot

    async def test_restart_preserves_message_id_and_resolution_is_terminal(self) -> None:
        """Verify restart preserves message id and resolution is terminal."""

        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        await self.store.close()
        self.store = await Store.open(self.path, now=1001)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        await self.store.ingest([alert("resolved")], now=1002)
        await self.delivery.once(now=1002)
        await self.store.ingest([alert()], now=1003)
        assert not await self.delivery.once(now=1003)
        assert len(self.telegram.messages) == 2
        assert "RESOLVED" in self.telegram.messages[1]["text"]

    async def test_delivery_failure_keeps_durable_work_and_retries(self) -> None:
        """Verify delivery failure keeps durable work and retries."""

        await self.store.ingest([alert()], now=1000)
        self.telegram.fail = True
        await self.delivery.once(now=1000)
        assert await self.store.pending_count() == 1
        self.telegram.fail = False
        assert not await self.delivery.once(now=1000)
        await self.delivery.once(now=1100)
        assert len(self.telegram.messages) == 1
        assert await self.store.pending_count() == 0

    async def test_invalid_persisted_payload_does_not_stop_other_incidents(self) -> None:
        """Retry an invalid stored alert while allowing other incidents to proceed."""

        await self.store.ingest([alert()], now=1000)
        await self.store.db.execute("UPDATE incidents SET payload=json_set(payload,'$.panelURL',NULL)")
        await self.store.db.commit()
        await self.store.ingest([alert(starts_at="2026-10-17T13:00:00Z")], now=1001)

        assert await self.delivery.once(now=1001)
        assert await self.delivery.once(now=1001)

        assert len(self.telegram.messages) == 1
        assert await self.store.pending_count() == 1

    async def test_retry_after_is_measured_from_failure_not_start_of_slow_request(self) -> None:
        """Verify retry after is measured from failure not start of slow request."""

        clock = [100]

        async def limited(**_kwargs: object) -> NoReturn:
            """Simulate a Telegram rate limit with a retry delay."""

            clock[0] = 125
            raise RetryAfter(30)

        await self.store.ingest([alert()], now=1000)

        with (
            patch("grafana_telegram_relay.delivery.monotonic", side_effect=lambda: clock[0]),
            patch.dict(os.environ, PTB_TIMEDELTA="true"),
            patch.object(self.telegram, "send_message", limited),
        ):
            await self.delivery.once(now=1000)

        assert await self.store.pending(now=1031) is None
        assert await self.store.pending(now=1056) is not None

    async def test_retry_after_pauses_other_and_new_incidents(self) -> None:
        """Verify retry after pauses other and new incidents."""

        async def limited(**_kwargs: object) -> NoReturn:
            """Simulate a Telegram rate limit with a retry delay."""

            raise RetryAfter(30)

        await self.store.ingest([alert()], now=1000)

        with (
            patch.dict(os.environ, PTB_TIMEDELTA="true"),
            patch.object(self.telegram, "send_message", limited),
        ):
            await self.delivery.once(now=1000)

        await self.store.ingest([alert(starts_at="2026-10-17T13:00:00Z")], now=1001)
        assert await self.store.pending(now=1002) is None
        assert await self.store.pending(now=1031) is not None

    async def test_photo_is_edited_and_render_failure_does_not_drop_resolution(self) -> None:
        """Verify photo is edited and render failure does not drop resolution."""

        async def render(item: Alert) -> bytes | None:
            """Provide the screenshot result required by this delivery scenario."""

            return b"PNG" if item.status == "firing" else None

        self.delivery.render = render
        firing = alert()
        firing.labels["mention"] = "@alice @bob"
        resolved = alert("resolved")
        resolved.labels["mention"] = "@alice @bob"

        await self.store.ingest([firing], now=1000)
        await self.delivery.once(now=1000)

        assert self.telegram.messages[1]["photo"] == b"PNG"
        assert self.telegram.messages[1]["parse_mode"] == "HTML"
        assert self.telegram.messages[1]["caption"].splitlines()[1] == "@alice @bob"

        await self.store.ingest([resolved], now=1001)
        await self.delivery.once(now=1001)

        assert len(self.telegram.messages) == 2
        assert "RESOLVED" in self.telegram.messages[1]["caption"]
        assert self.telegram.messages[1]["parse_mode"] == "HTML"
        assert self.telegram.messages[1]["caption"].splitlines()[1] == "@alice @bob"
        assert self.telegram.messages[2]["text"] == "🟢 Решено\n@alice @bob"
        parameters = self.telegram.messages[2]["reply_parameters"]
        assert parameters is not None
        assert parameters.message_id == 1

    async def test_custom_html_template_is_used_for_send_and_edit(self) -> None:
        """Verify custom html template is used for send and edit."""

        root = Path(self.directory.name)
        (root / "custom.html.j2").write_text("<b>{{ status }}</b> {{ labels.alertname }}")
        self.delivery.templates = MessageTemplates(root)
        item = alert()
        item.annotations["telegram_template"] = "custom.html.j2"
        await self.store.ingest([item], now=1000)
        await self.delivery.once(now=1000)
        assert self.telegram.messages[1]["text"] == "<b>firing</b> GrafanaTest"
        assert self.telegram.messages[1]["parse_mode"] == "HTML"
        item = alert("resolved")
        item.annotations["telegram_template"] = "custom.html.j2"
        await self.store.ingest([item], now=1001)
        await self.delivery.once(now=1001)
        assert len(self.telegram.messages) == 2
        assert self.telegram.messages[1]["text"] == "<b>resolved</b> GrafanaTest"
        assert self.telegram.messages[2]["text"] == "🟢 Решено"

    async def test_telegram_rejected_html_falls_back_to_plain_text(self) -> None:
        """Verify telegram rejected html falls back to plain text."""

        root = Path(self.directory.name)
        (root / "broken.html.j2").write_text("<b>{{ labels.alertname }}")
        self.delivery.templates = MessageTemplates(root)
        original = self.telegram.send_message

        async def send(
            *, chat_id: ChatId, text: str, parse_mode: str | None, reply_markup: InlineKeyboardMarkup | None = None
        ) -> SentMessage:
            """Simulate Telegram rejecting malformed HTML while accepting plain text."""

            if parse_mode == "HTML":
                msg = "Can't parse entities: can't find end tag"
                raise BadRequest(msg)

            return await original(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

        item = alert()
        item.annotations["telegram_template"] = "broken.html.j2"
        await self.store.ingest([item], now=1000)

        with patch.object(self.telegram, "send_message", send):
            await self.delivery.once(now=1000)

        assert self.telegram.messages[1]["text"] == "GrafanaTest"
        assert self.telegram.messages[1].get("parse_mode") is None
        assert await self.store.pending_count() == 0

    async def test_photo_caption_budget_is_used_for_custom_template(self) -> None:
        """Verify photo caption budget is used for custom template."""

        root = Path(self.directory.name)
        (root / "long.txt.j2").write_text("{{ 'x' * 2000 }}")
        self.delivery.templates = MessageTemplates(root)

        async def render(_item: Alert) -> bytes | None:
            """Provide the screenshot result required by this delivery scenario."""

            return b"PNG"

        self.delivery.render = render
        item = alert()
        item.annotations["telegram_template"] = "long.txt.j2"
        await self.store.ingest([item], now=1000)
        await self.delivery.once(now=1000)
        assert len(self.telegram.messages[1]["caption"]) == 1024

    async def test_resolution_during_send_is_delivered_in_next_iteration(self) -> None:
        """Verify resolution during send is delivered in next iteration."""

        async def render(item: Alert) -> bytes | None:
            """Provide the screenshot result required by this delivery scenario."""

            if item.status == "firing":
                await self.store.ingest([alert("resolved")], now=1001)

            return None

        self.delivery.render = render
        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        await self.delivery.once(now=1002)
        assert len(self.telegram.messages) == 2
        assert "RESOLVED" in self.telegram.messages[1]["text"]

    async def test_invalid_batch_is_rejected_atomically(self) -> None:
        """Verify invalid batch is rejected atomically."""

        with pytest.raises(ValueError, match="alertname is required"):
            await self.store.ingest([alert(), {"status": "firing"}], now=1000)

        assert await self.store.pending_count() == 0

    async def test_canary_timeout_recovery_and_restart(self) -> None:
        """Verify canary timeout recovery and restart."""

        await self.store.check_canary(now=1179, max_silence=180)
        assert await self.store.pending_count() == 0
        await self.store.check_canary(now=1181, max_silence=180)
        await self.delivery.once(now=1181)
        assert "GrafanaCanaryMissing" in self.telegram.messages[1]["text"]
        await self.store.close()
        self.store = await Store.open(self.path, now=1190)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        await self.store.check_canary(now=1200, max_silence=180)
        assert not await self.delivery.once(now=1200)
        await self.store.heartbeat(now=1201)
        await self.delivery.once(now=1201)
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        await self.store.check_canary(now=1382, max_silence=180)
        await self.delivery.once(now=1382)
        assert len(self.telegram.messages) == 3

    async def test_disabling_canary_retains_closed_history_and_discards_unfinished_updates(self) -> None:
        """Retain delivered recoveries while suppressing old firing and queued resolved canaries."""

        await self.store.check_canary(now=1181, max_silence=180)
        await self.delivery.once(now=1181)
        await self.store.heartbeat(now=1182)
        closed = await self.store.pending(now=1182)
        assert closed is not None
        await self.delivery.once(now=1182)
        await self.store.check_canary(now=1363, max_silence=180)
        await self.delivery.once(now=1363)
        await self.store.disable_canary(now=1364)

        async with self.store.db.execute("SELECT key FROM incidents") as cursor:
            keys: list[object] = [row["key"] for row in await cursor.fetchall()]

        assert keys == [closed["key"]]
        assert not await self.delivery.once(now=1364)
        assert (await self.store.metrics())["active"] == 0
        await self.store.check_canary(now=1545, max_silence=180)
        await self.store.heartbeat(now=1546)
        assert await self.store.pending_count() == 1
        await self.store.disable_canary(now=1547)
        assert not await self.delivery.once(now=1547)
        await self.store.check_canary(now=1548, max_silence=180)
        assert await self.store.pending_count() == 0
        assert len(self.telegram.messages) == 3

    async def test_resolution_reply_retries_after_restart_and_repeated_webhooks_are_ignored(self) -> None:
        """Retry a failed reply durably without creating another incident message."""

        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        self.telegram.fail_replies = True
        await self.store.ingest([alert("resolved")], now=1001)
        await self.delivery.once(now=1001)

        assert len(self.telegram.messages) == 1
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        assert await self.store.pending_count() == 1

        await self.store.cleanup(before=2000)
        await self.store.close()
        self.store = await Store.open(self.path, now=1100)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        self.telegram.fail_replies = False
        await self.delivery.once(now=1100)

        assert len(self.telegram.messages) == 2
        assert self.telegram.messages[2]["text"] == "🟢 Решено"
        assert await self.store.pending_count() == 0

        await self.store.ingest([alert("resolved")], now=1101)
        await self.store.ingest([alert()], now=1102)

        assert not await self.delivery.once(now=1102)
        assert len(self.telegram.messages) == 2

    async def test_confirmed_resolution_reply_is_not_resent_after_completion_failure(self) -> None:
        """Remember a successful reply even if completing the incident update fails."""

        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        await self.store.ingest([alert("resolved")], now=1001)

        with patch.object(self.store, "sent", side_effect=RuntimeError("commit failed")):
            await self.delivery.once(now=1001)

        assert len(self.telegram.messages) == 2
        assert await self.store.pending_count() == 1

        await self.store.close()
        self.store = await Store.open(self.path, now=1100)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        await self.delivery.once(now=1100)

        assert len(self.telegram.messages) == 2
        assert await self.store.pending_count() == 0

    async def test_resolved_first_notification_does_not_reply_to_itself(self) -> None:
        """Send one full notification when no firing message was previously delivered."""

        await self.store.ingest([alert("resolved")], now=1000)
        await self.delivery.once(now=1000)

        assert len(self.telegram.messages) == 1
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        assert self.telegram.messages[1]["reply_parameters"] is None
        assert await self.store.pending_count() == 0

    async def test_deleted_original_is_replaced_once_when_resolution_reply_fails(self) -> None:
        """Retain the replacement message across a failed reply and process restart."""

        await self.store.ingest([alert()], now=1000)
        await self.delivery.once(now=1000)
        del self.telegram.messages[1]
        self.telegram.fail_replies = True
        await self.store.ingest([alert("resolved")], now=1001)
        await self.delivery.once(now=1001)

        assert set(self.telegram.messages) == {2}
        assert "RESOLVED" in self.telegram.messages[2]["text"]
        assert await self.store.pending_count() == 1

        await self.store.close()
        self.store = await Store.open(self.path, now=1100)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        self.telegram.fail_replies = False
        await self.delivery.once(now=1100)

        assert set(self.telegram.messages) == {2, 3}
        assert self.telegram.messages[3]["text"] == "🟢 Решено"
        parameters = self.telegram.messages[3]["reply_parameters"]
        assert parameters is not None
        assert parameters.message_id == 2
        assert await self.store.pending_count() == 0

    async def test_retention_removes_only_delivered_closed_incidents(self) -> None:
        """Keep old active and undelivered incidents while expiring delivered recoveries."""

        expired = alert("resolved")
        active = alert()
        active.fingerprint = "active"
        pending = alert("resolved")
        pending.fingerprint = "pending"

        for item in (expired, active):
            await self.store.ingest([item], now=1000)
            await self.delivery.once(now=1000)

        await self.store.ingest([pending], now=1000)
        await self.store.cleanup(before=1100)

        async with self.store.db.execute("SELECT key FROM incidents ORDER BY key") as cursor:
            keys: list[object] = [row["key"] for row in await cursor.fetchall()]

        assert keys == [active.key, pending.key]
        assert await self.store.pending_count() == 1
