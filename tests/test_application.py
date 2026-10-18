"""Verify the production application lifecycle with isolated offline transports."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from telegram import Bot
from telegram.request import HTTPXRequest

from grafana_telegram_relay.app import STORE, TASKS, application
from grafana_telegram_relay.delivery import Delivery
from grafana_telegram_relay.store import Store
from tests.factories import alert, environment
from tests.fakes import Telegram, idle_updates


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise startup, readiness, and cleanup of the production workers."""

    async def test_production_lifecycle_starts_and_stops_workers_without_telegram(self) -> None:
        """Serve readiness and stop every worker without contacting Telegram."""

        with tempfile.TemporaryDirectory() as directory:
            values = environment(Path(directory))

            with (
                patch.dict(os.environ, values, clear=True),
                patch.object(Bot, "get_updates", idle_updates),
                patch.object(HTTPXRequest, "do_request") as telegram_request,
            ):
                app = await application()

                async with TestClient(TestServer(app)) as client:
                    assert (await client.get("/readyz")).status == 200
                    assert len(app[TASKS]) == 3
                    assert all(not task.done() for task in app[TASKS])
                    telegram_request.assert_not_called()

                assert all(task.cancelled() for task in app[TASKS])

    async def test_configured_retention_controls_background_cleanup(self) -> None:
        """Apply the environment's retention window to real persisted incident history."""

        with tempfile.TemporaryDirectory() as directory:
            values = environment(Path(directory))
            values["RETENTION_PERIOD"] = "1.5h"
            now = time.time()
            retained = alert("resolved")
            retained.fingerprint = "retained"
            expired = alert("resolved")
            expired.fingerprint = "expired"
            store = await Store.open(values["STATE_DB"], now=now)

            try:
                delivery = Delivery(store, Telegram(), "-123", None)

                for item, received in ((expired, now - 5500), (retained, now - 5300)):
                    await store.ingest([item], now=received)
                    await delivery.once(now=received)

            finally:
                await store.close()

            cleaned = asyncio.Event()
            original_cleanup = Store.cleanup

            async def cleanup(current: Store, before: float) -> None:
                """Signal completion after running the actual SQLite cleanup."""

                await original_cleanup(current, before)
                cleaned.set()

            with (
                patch.dict(os.environ, values, clear=True),
                patch.object(Bot, "get_updates", idle_updates),
                patch.object(HTTPXRequest, "do_request") as telegram_request,
                patch.object(Store, "cleanup", cleanup),
            ):
                app = await application()

                async with TestClient(TestServer(app)):
                    await asyncio.wait_for(cleaned.wait(), timeout=3)

                    async with app[STORE].db.execute("SELECT key FROM incidents ORDER BY key") as cursor:
                        keys: list[object] = [row["key"] for row in await cursor.fetchall()]

                    assert keys == [retained.key]
                    telegram_request.assert_not_called()
