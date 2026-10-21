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

    async def test_configured_reminder_interval_reaches_delivery_worker(self) -> None:
        """Apply the environment's repeat interval to the actual production worker."""

        with tempfile.TemporaryDirectory() as directory:
            values = environment(Path(directory))
            values["REMINDER_INTERVAL"] = "5m"
            observed: list[float] = []
            checked = asyncio.Event()

            async def once(delivery: Delivery, _now: float) -> bool:
                """Capture the configured interval before any outgoing Telegram operation."""

                observed.append(delivery.reminders.interval)
                checked.set()

                return False

            with (
                patch.dict(os.environ, values, clear=True),
                patch.object(Bot, "get_updates", idle_updates),
                patch.object(HTTPXRequest, "do_request") as telegram_request,
                patch.object(Delivery, "once", once),
            ):
                app = await application()

                async with TestClient(TestServer(app)):
                    await asyncio.wait_for(checked.wait(), timeout=3)

                    assert observed == [300]
                    telegram_request.assert_not_called()

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
                patch.object(Store, "check_canary") as check_canary,
            ):
                app = await application()

                async with TestClient(TestServer(app)):
                    await asyncio.wait_for(cleaned.wait(), timeout=3)

                    async with app[STORE].db.execute("SELECT key FROM incidents ORDER BY key") as cursor:
                        keys: list[object] = [row["key"] for row in await cursor.fetchall()]

                    assert keys == [retained.key]
                    check_canary.assert_not_called()
                    telegram_request.assert_not_called()

    async def test_enabled_canary_creates_an_outage_after_restart(self) -> None:
        """Run the optional watchdog against an expired persisted heartbeat."""

        with tempfile.TemporaryDirectory() as directory:
            values = environment(Path(directory))
            values["CANARY_ENABLED"] = "true"
            store = await Store.open(values["STATE_DB"], now=time.time() - 181)
            await store.close()
            checked = asyncio.Event()
            original_check = Store.check_canary

            async def check(current: Store, now: float, max_silence: float) -> float:
                """Signal completion after persisting a real watchdog incident."""

                last_seen = await original_check(current, now, max_silence)
                checked.set()
                return last_seen

            with (
                patch.dict(os.environ, values, clear=True),
                patch.object(Bot, "get_updates", idle_updates),
                patch.object(HTTPXRequest, "do_request") as telegram_request,
                patch.object(Store, "check_canary", check),
                patch.object(Delivery, "once", return_value=False),
            ):
                app = await application()

                async with TestClient(TestServer(app)):
                    await asyncio.wait_for(checked.wait(), timeout=3)
                    assert await app[STORE].pending_count() == 1
                    telegram_request.assert_not_called()

    async def test_disabled_canary_discards_unfinished_watchdog_incidents_before_delivery(self) -> None:
        """Suppress a saved canary outage while retaining ordinary pending alerts."""

        with tempfile.TemporaryDirectory() as directory:
            values = environment(Path(directory))
            store = await Store.open(values["STATE_DB"], now=1000)

            try:
                await store.check_canary(now=1200, max_silence=180)
                await store.ingest([alert()], now=1200)

            finally:
                await store.close()

            with (
                patch.dict(os.environ, values, clear=True),
                patch.object(Bot, "get_updates", idle_updates),
                patch.object(HTTPXRequest, "do_request") as telegram_request,
                patch.object(Delivery, "once", return_value=False),
            ):
                app = await application()

                async with TestClient(TestServer(app)) as client:
                    row = await app[STORE].pending(now=time.time())
                    assert row is not None
                    assert row["key"] == alert().key
                    assert await app[STORE].pending_count() == 1
                    assert (await app[STORE].metrics())["active"] == 1
                    assert (await client.post("/webhook/canary", json={})).status == 404
                    await app[STORE].heartbeat(now=time.time())
                    assert await app[STORE].pending_count() == 1
                    telegram_request.assert_not_called()
