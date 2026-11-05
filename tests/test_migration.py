"""Verify additive grouping migration against a database created using the old schema."""

import tempfile
import unittest
from pathlib import Path

from grafana_telegram_relay.models import acknowledgement_token
from grafana_telegram_relay.store import Store
from tests.factories import legacy_database


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    """Open old SQLite files using the production migration path."""

    async def test_old_database_keeps_ids_ack_retry_deadlines_and_cleanup(self) -> None:
        """Upgrade persisted state in place and retain legacy callback and cleanup behavior."""

        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "old.db"
        legacy_database(path)
        store = await Store.open(path, now=1100)

        try:
            assert await store.pending(1999) is None
            row = await store.pending(2000)
            assert row is not None
            assert row["message_id"] == 91
            assert row["resolution_message_id"] == 92
            assert row["attempts"] == 2
            assert row["photo"] == 1
            assert row["version"] == 5
            assert row["delivered"] == 4
            assert row["acknowledgement"] == {"user_id": 7, "name": "Alice", "at": 999.0, "version": 4}
            assert (
                await store.acknowledge(91, acknowledgement_token("legacy"), user_id=8, name="Bob", now=2000)
                == "already_acknowledged"
            )
            assert await store.reminder_task(1499) is None
            task = await store.reminder_task(1500)
            assert task is not None
            assert task["action"] == "delete"
            assert task["message_id"] == 93
            assert task["attempts"] == 2
            await store.reminder_deleted(93)
            assert await store.reminder_task(2499, 10) is None
            scheduled = await store.reminder_task(2500, 10)
            assert scheduled is not None
            assert scheduled["action"] == "send"
            assert scheduled["message_id"] == 94
            assert scheduled["attempts"] == 3
            await store.reminder_retry(scheduled, 2600)
            await store.close()
            store = await Store.open(path, now=2550)
            assert await store.reminder_task(2599, 10) is None
            retried = await store.reminder_task(2600, 10)
            assert retried is not None
            assert retried["message_id"] == 94
            assert retried["attempts"] == 4

        finally:
            await store.close()
            directory.cleanup()
