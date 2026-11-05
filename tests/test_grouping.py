"""Exercise durable group episodes, attention generations and explicitly resolved members."""

import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

import pytest
lazy from telegram import InlineKeyboardMarkup

from grafana_telegram_relay.delivery import Delivery, DeliveryOptions
from grafana_telegram_relay.grouping import notification
from grafana_telegram_relay.messages import MessageTemplates
from grafana_telegram_relay.models import Webhook
from grafana_telegram_relay.store import Store
from tests.factories import alert_payload
from tests.fakes import Telegram
lazy from grafana_telegram_relay.interfaces import ChatId
lazy from grafana_telegram_relay.models import Alert, AlertStatus
lazy from tests.fakes import SentMessage


def member(host: str, status: AlertStatus = "firing", *, start: str = "2026-10-17T12:00:00Z") -> dict[str, object]:
    """Provide member identities independently of aggregate implementation."""

    return alert_payload(status, starts_at=start, fingerprint=host, labels={"alertname": "CPU", "instance": host})


def group(*members: dict[str, object], receiver: str = "telegram") -> Webhook:
    """Build validated Grafana metadata for the incident lifecycle tests."""

    return Webhook.model_validate({"groupKey": "opaque", "receiver": receiver, "alerts": list(members)})


class GroupTests(unittest.IsolatedAsyncioTestCase):
    """Use real SQLite transactions with an offline Telegram transport."""

    @override
    async def asyncSetUp(self) -> None:
        """Open one independent durable queue."""

        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.db"
        self.store = await Store.open(self.path, now=1000)
        self.telegram = Telegram()
        self.delivery = Delivery(self.store, self.telegram, "-123", None, DeliveryOptions(reminder_interval=10))

    @override
    async def asyncTearDown(self) -> None:
        """Release the database and filesystem fixture."""

        await self.store.close()
        self.directory.cleanup()

    async def token(self, message: int = 1) -> str:
        """Read the callback actually visible to an operator."""

        markup = self.telegram.messages[message]["reply_markup"]
        assert markup is not None
        data = markup.inline_keyboard[0][0].callback_data
        assert isinstance(data, str)

        return data.removeprefix("ack:")

    async def test_partial_recovery_keeps_omitted_members_and_full_recovery_replies_once(self) -> None:
        """Require explicit recovery of every tracked episode, including after restart."""

        await self.store.ingest(group(member("a"), member("b")), now=1000)
        await self.delivery.once(1000)
        await self.store.ingest(group(member("a", "resolved")), now=1001)
        await self.delivery.once(1001)
        assert len(self.telegram.messages) == 1
        assert "FIRING" in self.telegram.messages[1]["text"]
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        assert "b" in self.telegram.messages[1]["text"]
        await self.store.close()
        self.store = await Store.open(self.path, now=1002)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        await self.store.ingest(group(member("a"), member("b", "resolved")), now=1003)
        await self.delivery.once(1003)
        assert len(self.telegram.messages) == 2
        assert self.telegram.messages[2]["text"] == "🟢 Решено"
        await self.store.ingest(group(member("a"), member("b")), now=1004)
        assert not await self.delivery.once(1004)
        await self.store.ingest(group(member("a", start="2026-10-17T13:00:00Z")), now=1005)
        await self.delivery.once(1005)
        assert len(self.telegram.messages) == 3

    async def test_new_member_revokes_ack_and_old_original_and_copy_controls_are_stale(self) -> None:
        """Reject controls that were rendered before new failures entered the group."""

        await self.store.ingest(group(member("a")), now=1000)
        await self.delivery.once(1000)
        old = await self.token()
        await self.delivery.once(1011)
        assert len(self.telegram.messages) == 2
        assert await self.store.acknowledge(1, old, user_id=7, name="Alice", now=1012) == "acknowledged"
        await self.store.ingest(group(member("b")), now=1013)
        assert await self.store.acknowledge(1, old, user_id=7, name="Alice", now=1013) == "not_found"
        assert await self.store.acknowledge(2, old, user_id=7, name="Alice", now=1013) == "not_found"
        await self.delivery.once(1013)
        await self.delivery.once(1013)
        assert 2 not in self.telegram.messages
        fresh = await self.token()
        assert fresh != old
        assert await self.store.acknowledge(1, fresh, user_id=8, name="Bob", now=1014) == "acknowledged"
        await self.delivery.once(1014)
        await self.store.ingest(group(member("a", "resolved")), now=1015)
        await self.delivery.once(1015)
        assert "Acknowledged by Bob" in self.telegram.messages[1]["text"]

    async def test_receiver_namespaces_groups_and_legacy_episodes_drain_in_place(self) -> None:
        """Preserve historical messages without guessing their former group identity."""

        await self.store.ingest([member("legacy")], now=1000)
        await self.delivery.once(1000)
        await self.store.ingest(group(member("legacy", "resolved"), member("new")), now=1001)
        await self.delivery.once(1001)
        await self.delivery.once(1001)
        assert "RESOLVED" in self.telegram.messages[1]["text"]
        assert len(self.telegram.messages) == 3
        await self.store.ingest(group(member("new"), receiver="other"), now=1002)
        await self.delivery.once(1002)
        assert len(self.telegram.messages) == 4

    async def test_singleton_photo_becomes_text_and_retired_photo_cleanup_survives_retry(self) -> None:
        """Persist text replacement and retire obsolete singleton controls before completion."""

        async def render(_item: Alert) -> bytes | None:
            """Provide the real delivery worker with an offline screenshot."""

            return b"PNG"

        self.delivery.render = render
        await self.store.ingest(group(member("a")), now=1000)
        await self.delivery.once(1000)
        old = await self.token()
        assert self.telegram.messages[1]["photo"] == b"PNG"
        await self.store.ingest(group(member("b")), now=1001)

        with patch.object(self.store, "sent", side_effect=RuntimeError("completion failed")):
            await self.delivery.once(1001)

        assert self.telegram.messages[2]["text"]
        assert await self.store.acknowledge(1, old, user_id=7, name="Alice", now=1002) == "not_found"
        await self.store.close()
        self.store = await Store.open(self.path, now=1100)
        self.delivery = Delivery(self.store, self.telegram, "-123", None)
        await self.delivery.once(1100)
        await self.delivery.once(1100)
        assert set(self.telegram.messages) == {2}
        assert await self.store.pending_count() == 0

    async def test_long_members_are_escaped_and_overflow_is_explicit(self) -> None:
        """Keep member identity/status visible and reserve space for omitted-member counts."""

        members = [member(f"host-{index}") for index in range(50)]
        members[0]["annotations"] = {"summary": "<unsafe>&" * 100}
        members[0]["labels"] = {"alertname": "<CPU>", "instance": "host-0", "mention": "@alice " * 1000}
        await self.store.ingest(group(*members), now=1000)
        await self.delivery.once(1000)
        text = self.telegram.messages[1]["text"]
        assert "members omitted" in text
        assert "&lt;CPU&gt;" in text
        assert len(text.encode("utf-16-le")) // 2 < 6000

    async def test_new_member_during_render_keeps_new_generation_pending(self) -> None:
        """Prevent stale send completion from acknowledging or consuming unseen failures."""

        async def render(_item: Alert) -> bytes | None:
            """Receive another webhook while the original snapshot is in flight."""

            await self.store.ingest(group(member("b")), now=1001)

            return None

        self.delivery.render = render
        await self.store.ingest(group(member("a")), now=1000)
        await self.delivery.once(1000)
        old = await self.token()
        assert await self.store.pending_count() == 1
        assert await self.store.acknowledge(1, old, user_id=7, name="Alice", now=1002) == "not_found"
        await self.delivery.once(1002)
        assert "instance=b" in self.telegram.messages[1]["text"]
        assert await self.store.pending_count() == 0

    async def test_stale_copy_completion_is_tracked_for_cleanup_with_repeats_disabled(self) -> None:
        """Retire copies selected before a new member arrived even if they finish later."""

        await self.store.ingest(group(member("a")), now=1000)
        await self.delivery.once(1000)
        original = self.telegram.copy_message

        async def copying(
            *, chat_id: ChatId, from_chat_id: ChatId, message_id: int, reply_markup: InlineKeyboardMarkup | None
        ) -> SentMessage:
            """Receive new group state during the external copy operation."""

            await self.store.ingest(group(member("b")), now=1012)

            return await original(
                chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, reply_markup=reply_markup
            )

        with patch.object(self.telegram, "copy_message", copying):
            await self.delivery.once(1011)

        old = await self.token(2)
        assert await self.store.acknowledge(2, old, user_id=7, name="Alice", now=1013) == "not_found"
        self.delivery.reminders.interval = 0
        await self.delivery.once(1014)
        await self.delivery.once(1014)
        assert 2 not in self.telegram.messages
        assert "instance=b" in self.telegram.messages[1]["text"]

    async def test_failed_group_transaction_leaves_no_partial_members_or_incident(self) -> None:
        """Roll back both queue and member bookkeeping when snapshot assembly fails."""

        calls = 0

        def fail_final(members: list[Alert], webhook: Webhook) -> Alert:
            """Fail after the initial incident and member rows have been inserted."""

            nonlocal calls
            calls += 1

            if calls > 1:
                msg = "snapshot assembly failed"
                raise RuntimeError(msg)

            return notification(members, webhook)

        with (
            patch("grafana_telegram_relay.store.notification", side_effect=fail_final),
            pytest.raises(RuntimeError, match="snapshot assembly failed"),
        ):
            await self.store.ingest(group(member("a"), member("b")), now=1000)

        assert await self.store.pending_count() == 0
        await self.store.ingest(group(member("a"), member("b")), now=1001)
        await self.delivery.once(1001)
        assert "instance=a" in self.telegram.messages[1]["text"]
        assert "instance=b" in self.telegram.messages[1]["text"]

    async def test_common_prose_is_shown_once_and_normal_six_host_group_fits(self) -> None:
        """Keep shared remediation separate from compact member identities."""

        members = [member(f"host-{index}") for index in range(6)]

        for item in members:
            item["annotations"] = {"summary": "Reboot pending", "description": "Install updates and reboot safely."}

        await self.store.ingest(group(*members), now=1000)
        await self.delivery.once(1000)
        text = self.telegram.messages[1]["text"]
        assert all(f"instance=host-{index}" in text for index in range(6))
        assert text.count("Install updates and reboot safely.") == 1
        assert "members omitted" not in text

    async def test_failed_group_templates_preserve_one_mention_below_header(self) -> None:
        """Keep operator notification and overflow visible after missing or broken templates."""

        root = Path(self.directory.name)
        (root / "broken.html.j2").write_text("{{ undefined.value }}")
        self.delivery.templates = MessageTemplates(root)

        for index, template in enumerate(("missing.html.j2", "broken.html.j2"), start=1):
            members = [
                alert_payload(
                    fingerprint=f"host-{host}",
                    labels={"alertname": "CPU", "instance": f"host-{host}", "mention": "@alice"},
                    annotations={"summary": "CPU high", "telegram_template": template},
                )
                for host in range(40)
            ]
            webhook = group(*members).model_copy(update={"group_key": template})
            await self.store.ingest(webhook, now=1000)
            await self.delivery.once(1000)
            text = self.telegram.messages[index]["text"]
            assert text.splitlines()[1] == "@alice"
            assert text.count("@alice") == 1
            assert "members omitted" in text
            assert len(text.encode("utf-16-le")) // 2 <= 4096

    async def test_clipped_host_collisions_remain_distinct_during_partial_recovery(self) -> None:
        """Show episode identifiers when different raw labels render as the same identity."""

        prefix = "service-" + "a" * 165
        cases = (
            ({"instance": prefix + "-left"}, {"instance": prefix + "-right"}),
            (
                {"instance": "same-host", "aaa": "a" * 160, "bbb": "b" * 160, "zzz": "left"},
                {"instance": "same-host", "aaa": "a" * 160, "bbb": "b" * 160, "zzz": "right"},
            ),
        )

        for index, (left_labels, right_labels) in enumerate(cases, start=1):
            left = alert_payload(fingerprint="left", labels={"alertname": "CPU", **left_labels})
            right = alert_payload(fingerprint="right", labels={"alertname": "CPU", **right_labels})
            webhook = group(left, right).model_copy(update={"group_key": f"collision-{index}"})
            await self.store.ingest(webhook, now=1000)
            await self.delivery.once(1000)
            right["status"] = "resolved"
            right["endsAt"] = "2026-10-17T12:10:00Z"
            update = group(right).model_copy(update={"group_key": f"collision-{index}"})
            await self.store.ingest(update, now=1001)
            await self.delivery.once(1001)
            text = self.telegram.messages[index]["text"]
            assert "left · 2026-10-17T12:00:00+00:00" in text
            assert "right · 2026-10-17T12:00:00+00:00" in text
            assert "…" in text
            assert text.index("FIRING</b>") < text.index("left ·") < text.index("RESOLVED</b>") < text.index("right ·")
            assert len(self.telegram.messages) == index

    async def test_all_group_templates_preserve_member_query_values(self) -> None:
        """Retain useful bounded values with safe technical formatting for every builtin."""

        templates = ("default.html.j2", "compact.html.j2", "default.txt.j2", "missing.html.j2")

        for index, template in enumerate(templates, start=1):
            members = [
                alert_payload(
                    fingerprint=host,
                    labels={"alertname": "CPU", "instance": host},
                    annotations={"telegram_template": template},
                    values={"A": value, "B": "<query>&" * 100},
                )
                for host, value in (("metric-a", 98.765432), ("metric-b", 87.654321))
            ]
            await self.store.ingest(group(*members).model_copy(update={"group_key": template}), now=1000)
            await self.delivery.once(1000)
            text = self.telegram.messages[index]["text"]
            assert "98.765432" in text
            assert "87.654321" in text
            assert "Values:" in text
            assert "…" in text
            assert len(text.encode("utf-16-le")) // 2 <= 4096

            if template != "missing.html.j2" and template.endswith("html.j2"):
                assert "<code>" in text
                assert "&lt;query&gt;&amp;" in text

    async def test_retention_prunes_dead_group_heads_and_preserves_active_and_orphan_cleanup(self) -> None:
        """Bound group bookkeeping without deleting active state or pending reminder deletion."""

        await self.store.ingest(group(member("closed", "resolved")), now=1000)
        await self.delivery.once(1000)
        await self.store.ingest(group(member("active"), receiver="active"), now=1001)
        await self.delivery.once(1001)
        await self.store.db.execute(
            "INSERT INTO reminder_messages(message_id,incident_key,sent_at) VALUES(93,'expired',10)"
        )
        await self.store.db.commit()
        await self.store.cleanup(before=1100)

        async with self.store.db.execute("SELECT count(*) FROM group_heads") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1

        assert (await self.store.metrics())["active"] == 1
        task = await self.store.reminder_task(1100)
        assert task is not None
        assert task["action"] == "delete"
        assert task["message_id"] == 93

    async def test_bounded_display_preserves_complete_raw_member_values_for_custom_templates(self) -> None:
        """Keep the raw alerts context complete while builtin query summaries are bounded."""

        root = Path(self.directory.name)
        (root / "raw.txt.j2").write_text("{{ alerts[0]['values']['A'] }} {{ alerts[1]['values']['B']|length }}")
        self.delivery.templates = MessageTemplates(root)
        members = [
            alert_payload(
                fingerprint=host,
                labels={"alertname": "CPU", "instance": host},
                annotations={"telegram_template": "raw.txt.j2"},
                values={"A": value, "B": "<query>&" * 100},
            )
            for host, value in (("metric-a", 98.765432), ("metric-b", 87.654321))
        ]
        await self.store.ingest(group(*members), now=1000)
        await self.delivery.once(1000)
        assert self.telegram.messages[1]["text"] == "98.765432 800"
