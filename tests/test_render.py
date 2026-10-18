"""Verify authenticated Grafana rendering against local HTTP test servers."""

import asyncio
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from grafana_telegram_relay.delivery import Delivery
from grafana_telegram_relay.render import Renderer
from grafana_telegram_relay.store import Store
from tests.factories import alert
from tests.fakes import Telegram


class RendererTests(unittest.IsolatedAsyncioTestCase):
    """Exercise screenshot headers, parameters, responses, and stream handling."""

    async def test_renderer_uses_configured_host_and_panel_variables(self) -> None:
        """Verify renderer uses configured host and panel variables."""

        received: list[tuple[str, dict[str, str], str | None]] = []

        async def image(request: web.Request) -> web.Response:
            """Serve a local screenshot response and capture render request parameters."""

            received.append((request.path, dict(request.query), request.headers.get("Authorization")))
            return web.Response(body=b"PNG", content_type="image/png")

        server_app = web.Application()
        server_app.router.add_get("/grafana/render/d-solo/{uid}/{slug}", image)

        async with TestServer(server_app) as server, aiohttp.ClientSession() as session:
            renderer = Renderer(session, str(server.make_url("/grafana")), "secret")
            assert await renderer(alert()) is None
            assert received == []
            item = alert()
            item.panel_url = "http://untrusted.invalid/grafana/d/test-uid?orgId=1&viewPanel=2"
            item.labels.update(compose_project="observability", instance="grafana")
            assert await renderer(item) == b"PNG"

        assert received[0][0] == "/grafana/render/d-solo/test-uid/_"
        assert received[0][1]["panelId"] == "2"
        assert received[0][1]["var-project"] == "observability"
        assert received[0][1]["var-instance"] == "grafana"
        assert received[0][2] == "Bearer secret"

    async def test_render_failure_falls_back_to_delivered_text(self) -> None:
        """Complete incident delivery as text when the screenshot endpoint is unavailable."""

        async def unavailable(_request: web.Request) -> web.Response:
            """Simulate an unavailable Grafana rendering endpoint."""

            return web.Response(status=503)

        app = web.Application()
        app.router.add_get("/render/d-solo/{uid}/{slug}", unavailable)

        with tempfile.TemporaryDirectory() as directory:
            store = await Store.open(Path(directory) / "state.db", now=1000)

            try:
                async with TestServer(app) as server, aiohttp.ClientSession() as session:
                    renderer = Renderer(session, str(server.make_url("")), "secret")
                    telegram = Telegram()
                    delivery = Delivery(store, telegram, "-123", renderer)
                    item = alert()
                    item.annotations.update(__dashboardUid__="monitoring-grafana", __panelId__="1")
                    await store.ingest([item], now=1000)
                    await delivery.once(now=1000)

                assert len(telegram.messages) == 1
                assert "GrafanaTest" in telegram.messages[1]["text"]
                assert "photo" not in telegram.messages[1]
                assert await store.pending_count() == 0

            finally:
                await store.close()

    async def test_renderer_reads_entire_streamed_image(self) -> None:
        """Verify renderer reads entire streamed image."""

        async def image(request: web.Request) -> web.StreamResponse:
            """Serve a local screenshot response and capture render request parameters."""

            response = web.StreamResponse(headers={"Content-Type": "image/png"})
            await response.prepare(request)
            await response.write(b"first")
            await asyncio.sleep(0.02)
            await response.write(b"second")
            return response

        app = web.Application()
        app.router.add_get("/render/d-solo/{uid}/{slug}", image)

        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            renderer = Renderer(session, str(server.make_url("")), "secret")
            item = alert()
            item.annotations.update(__dashboardUid__="test", __panelId__="1")
            assert await renderer(item) == b"firstsecond"

    async def test_renderer_selects_all_option_when_alert_has_no_instance_label(self) -> None:
        """Verify renderer selects all option when alert has no instance label."""

        received: list[dict[str, str]] = []

        async def image(request: web.Request) -> web.Response:
            """Serve a local screenshot response and capture render request parameters."""

            received.append(dict(request.query))
            return web.Response(body=b"PNG", content_type="image/png")

        app = web.Application()
        app.router.add_get("/render/d-solo/{uid}/{slug}", image)

        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            renderer = Renderer(session, str(server.make_url("")), "secret")
            item = alert()
            del item.labels["instance"]
            item.annotations.update(__dashboardUid__="test", __panelId__="1")
            await renderer(item)

        assert received[0]["var-instance"] == "$__all"
