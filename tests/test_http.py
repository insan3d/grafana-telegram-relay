"""Verify webhook validation and operational HTTP endpoints against SQLite."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import override

from aiohttp.test_utils import TestClient, TestServer
from prometheus_client.parser import text_string_to_metric_families

from grafana_telegram_relay.app import TASKS, create_app
from grafana_telegram_relay.models import Alert
from grafana_telegram_relay.store import Store
from tests.factories import alert, alert_payload
lazy from grafana_telegram_relay.models import AlertStatus

type MetricKey = tuple[str, tuple[tuple[str, str], ...]]


class HttpTests(unittest.IsolatedAsyncioTestCase):
    """Exercise webhook persistence, readiness, liveness, and metrics through HTTP."""

    @override
    async def asyncSetUp(self) -> None:
        """Create an isolated database and transport for each test."""

        self.directory = tempfile.TemporaryDirectory()
        self.store = await Store.open(Path(self.directory.name) / "state.db", now=1000)
        self.app = create_app(self.store, canary_enabled=True)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    @override
    async def asyncTearDown(self) -> None:
        """Close asynchronous resources and remove temporary data."""

        await self.client.close()
        await self.store.close()
        self.directory.cleanup()

    async def samples(self) -> dict[MetricKey, float]:
        """Collect current Prometheus sample values from the HTTP endpoint."""

        response = await self.client.get("/metrics")
        assert response.status == 200
        return {
            (sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in text_string_to_metric_families(await response.text())
            for sample in family.samples
        }

    async def test_readiness_requires_writable_database_while_liveness_stays_up(self) -> None:
        """Verify readiness requires writable database while liveness stays up."""

        assert (await self.client.get("/readyz")).status == 200
        await self.store.db.execute("PRAGMA query_only=ON")
        assert (await self.client.get("/readyz")).status == 503
        assert (await self.client.get("/healthz")).status == 200
        await self.store.db.execute("PRAGMA query_only=OFF")
        assert (await self.client.get("/readyz")).status == 200

    async def test_readiness_fails_if_background_worker_stops(self) -> None:
        """Verify readiness fails if background worker stops."""

        worker: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.app[TASKS].append(worker)
        assert (await self.client.get("/readyz")).status == 200
        worker.cancel()
        assert (await self.client.get("/readyz")).status == 503
        assert (await self.client.get("/healthz")).status == 200

    async def test_metrics_count_webhook_results_and_accepted_alerts(self) -> None:
        """Verify metrics count webhook results and accepted alerts."""

        before = await self.samples()
        key: MetricKey
        await self.client.post("/webhook/grafana", json={"alerts": [alert_payload()]})
        await self.client.post("/webhook/grafana", json={})
        after = await self.samples()

        for status in ("202", "400"):
            key = (
                "telegram_bot_http_requests_total",
                (
                    ("method", "POST"),
                    ("route", "/webhook/grafana"),
                    ("status", status),
                ),
            )
            assert after[key] - before.get(key, 0) == 1

        key = ("telegram_bot_webhook_alerts_total", (("endpoint", "grafana"),))
        assert after[key] - before.get(key, 0) == 1
        key = (
            "telegram_bot_http_request_duration_seconds_count",
            (
                ("method", "POST"),
                ("route", "/webhook/grafana"),
            ),
        )
        assert after[key] - before.get(key, 0) == 2

    async def test_metrics_show_durable_queue_and_canary_state(self) -> None:
        """Verify metrics show durable queue and canary state."""

        await self.store.ingest([alert()], now=1000)
        metrics = await self.samples()
        assert metrics["telegram_bot_pending_incidents", ()] == 1
        assert metrics["telegram_bot_active_incidents", ()] == 1
        assert metrics["telegram_bot_canary_enabled", ()] == 1
        assert metrics["telegram_bot_canary_last_seen_seconds", ()] == 1000
        assert metrics["telegram_bot_canary_healthy", ()] == 0
        await self.store.heartbeat(now=time.time())
        metrics = await self.samples()
        assert metrics["telegram_bot_canary_healthy", ()] == 1

    async def test_default_app_disables_canary_webhook_and_metrics(self) -> None:
        """Keep ordinary webhooks usable without accepting or reporting canary heartbeats."""

        async with TestClient(TestServer(create_app(self.store))) as client:
            item = alert_payload(labels={"alertname": "GrafanaCanary", "canary": "true"})
            assert (await client.post("/webhook/canary", json={"alerts": [item]})).status == 404
            assert (await self.store.metrics())["last_seen"] == 1000
            assert await self.store.pending_count() == 0
            assert (await client.post("/webhook/grafana", json={"alerts": [alert_payload()]})).status == 202
            response = await client.get("/metrics")
            metrics = {
                sample.name: sample.value
                for family in text_string_to_metric_families(await response.text())
                for sample in family.samples
            }

            assert metrics["telegram_bot_canary_enabled"] == 0
            assert metrics["telegram_bot_canary_healthy"] == 0
            assert metrics["telegram_bot_canary_last_seen_seconds"] == 0
            assert metrics["telegram_bot_canary_timeout_seconds"] == 0
            assert metrics["telegram_bot_pending_incidents"] == 1

    async def test_metrics_bound_unknown_paths_and_exclude_probes(self) -> None:
        """Verify metrics bound unknown paths and exclude probes."""

        before = await self.samples()

        for path in ("/healthz", "/readyz", "/metrics", "/unknown-one", "/unknown-two"):
            await self.client.get(path)

        after = await self.samples()
        key = (
            "telegram_bot_http_requests_total",
            (
                ("method", "GET"),
                ("route", "unmatched"),
                ("status", "404"),
            ),
        )
        assert after[key] - before.get(key, 0) == 2

        for name, labels in after:
            if name == "telegram_bot_http_requests_total":
                assert dict(labels)["route"] in ("/webhook/grafana", "/webhook/canary", "unmatched")

    async def test_webhook_acknowledges_persisted_alerts_and_rejects_invalid_payload(self) -> None:
        """Verify webhook acknowledges persisted alerts and rejects invalid payload."""

        response = await self.client.post("/webhook/grafana", json={"alerts": [alert_payload()]})
        assert response.status == 202
        assert await self.store.pending_count() == 1
        invalid: tuple[object, ...] = ({}, {"alerts": [None]}, [], {"alerts": [alert_payload(), {}]})

        for body in invalid:
            response = await self.client.post("/webhook/grafana", json=body)
            assert response.status == 400

        assert await self.store.pending_count() == 1

    async def test_only_valid_firing_canary_refreshes_heartbeat(self) -> None:
        """Verify only valid firing canary refreshes heartbeat."""

        rejected: tuple[tuple[str, AlertStatus], ...] = (
            ("DatasourceError", "firing"),
            ("GrafanaCanary", "resolved"),
        )

        for name, status in rejected:
            item = alert_payload(status, labels={"alertname": name, "canary": "true"})
            response = await self.client.post("/webhook/canary", json={"alerts": [item]})
            assert response.status == 400

        await self.store.check_canary(now=1200, max_silence=180)
        assert await self.store.pending_count() == 1
        item = alert_payload(labels={"alertname": "GrafanaCanary", "canary": "true"})
        response = await self.client.post("/webhook/canary", json={"alerts": [item]})
        assert response.status == 202
        row = await self.store.pending(now=9999999999)
        assert row is not None
        assert Alert.model_validate_json(row["payload"]).status == "resolved"

    async def test_truncated_webhook_does_not_queue_alerts(self) -> None:
        """Reject a partial Grafana notification before persisting its entries."""

        response = await self.client.post("/webhook/grafana", json={"alerts": [alert_payload()], "truncatedAlerts": 1})

        assert response.status == 400
        assert await self.store.pending_count() == 0

    async def test_invalid_canary_batch_does_not_refresh_heartbeat(self) -> None:
        """Validate the complete batch before updating the durable heartbeat."""

        item = alert_payload(labels={"alertname": "GrafanaCanary", "canary": "true"})
        response = await self.client.post("/webhook/canary", json={"alerts": [item, alert_payload("resolved")]})

        assert response.status == 400
        assert (await self.store.metrics())["last_seen"] == 1000
        assert await self.store.pending_count() == 0

    async def test_invalid_resolved_timestamp_cannot_poison_delivery_queue(self) -> None:
        """Verify invalid resolved timestamp cannot poison delivery queue."""

        item = alert_payload("resolved", endsAt="not-a-date")
        response = await self.client.post("/webhook/grafana", json={"alerts": [item]})

        assert response.status == 400
        assert await self.store.pending_count() == 0

    async def test_webhook_rejects_invalid_typed_fields_before_queueing(self) -> None:
        """Verify webhook rejects invalid typed fields before queueing."""

        for field, value in (("values", []), ("panelURL", 42), ("labels", {"alertname": 123})):
            item: dict[str, object] = alert_payload()
            item[field] = value

            response = await self.client.post("/webhook/grafana", json={"alerts": [item]})

            assert response.status == 400

        assert await self.store.pending_count() == 0

    async def test_nonfinite_nested_results_do_not_enter_the_queue(self) -> None:
        """Reject non-JSON query numbers before accepting any incident in a batch."""

        for value in (float("nan"), float("inf"), float("-inf")):
            item = alert_payload(values={"A": [value]})
            response = await self.client.post("/webhook/grafana", json={"alerts": [alert_payload(), item]})

            assert response.status == 400

        assert await self.store.pending_count() == 0

    async def test_webhook_rejects_noninteger_truncation_metadata(self) -> None:
        """Reject falsy malformed truncation metadata before persisting alerts."""

        response = await self.client.post(
            "/webhook/grafana", json={"alerts": [alert_payload()], "truncatedAlerts": False}
        )

        assert response.status == 400
        assert await self.store.pending_count() == 0
