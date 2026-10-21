"""
Serve Grafana webhooks, operational endpoints, and background workers.

Webhook responses follow durable persistence. Readiness checks local workers and
SQLite independently of the Grafana and Telegram outages the bot must report.
"""

import asyncio
import contextlib
import logging
import time
from time import monotonic
lazy from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import aiosqlite
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from telegram import Bot
from telegram.request import HTTPXRequest

from .callbacks import Callbacks
from .delivery import Delivery, DeliveryOptions
from .messages import MessageTemplates
from .metrics import (
    ACTIVE,
    CANARY_ENABLED,
    CANARY_HEALTHY,
    CANARY_SEEN,
    CANARY_TIMEOUT,
    HTTP_DURATION,
    HTTP_REQUESTS,
    OLDEST_PENDING,
    PENDING,
    WEBHOOK_ALERTS,
)
from .models import Webhook
from .render import Renderer
from .settings import Settings
from .store import Store

STORE = web.AppKey("store", Store)
TASKS = web.AppKey[list[asyncio.Future[None]]]("tasks")
log = logging.getLogger(__name__)


@web.middleware
async def observe_http(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """
    Measure bounded HTTP routes while excluding probes and scrapes.

    Args:
        request: Incoming HTTP request.
        handler: Next handler in the aiohttp middleware chain.

    Returns:
        The response produced by the next handler.
    """

    if request.path in ("/healthz", "/readyz", "/metrics"):
        return await handler(request)

    route = request.path if request.path in ("/webhook/grafana", "/webhook/canary") else "unmatched"
    method = (
        request.method
        if request.method
        in (
            "GET",
            "HEAD",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
        )
        else "OTHER"
    )
    started, status = monotonic(), 500

    try:
        response = await handler(request)
        status = response.status

    except web.HTTPException as error:
        status = error.status
        raise

    else:
        return response

    finally:
        HTTP_REQUESTS.labels(route, method, str(status)).inc()
        HTTP_DURATION.labels(route, method).observe(monotonic() - started)


def create_app(store: Store, canary_timeout: float = 180, *, canary_enabled: bool = False) -> web.Application:
    """
    Build the webhook and operational HTTP routes.

    Args:
        store: Open persistent incident store.
        canary_timeout: Allowed heartbeat silence in seconds.
        canary_enabled: Whether to accept heartbeats and expose active canary state.

    Returns:
        An application ready for worker lifecycle registration or HTTP tests.
    """

    app = web.Application(client_max_size=1024 * 1024, middlewares=[observe_http])
    app[STORE] = store
    app[TASKS] = []

    async def webhook(request: web.Request) -> web.Response:
        """Validate and persist alerts before returning an acknowledgement."""

        canary = request.path == "/webhook/canary"

        try:
            body = Webhook.model_validate_json(await request.read())

            if canary:
                body.require_canary()
                await store.heartbeat(time.time())

            else:
                await store.ingest(body.alerts, time.time())

        except ValueError, TypeError, AttributeError:
            raise web.HTTPBadRequest(text="Invalid Grafana webhook payload") from None

        WEBHOOK_ALERTS.labels(request.path.rsplit("/", 1)[-1]).inc(len(body.alerts))
        return web.Response(status=202)

    async def health(_request: web.Request) -> web.Response:
        """Confirm that the HTTP process can respond."""

        return web.Response(text="ok\n")

    async def ready(_request: web.Request) -> web.Response:
        """Require running workers and a writable incident database."""

        if any(task.done() for task in app[TASKS]):
            raise web.HTTPServiceUnavailable(text="Background task stopped")

        try:
            await store.ready()

        except aiosqlite.Error:
            raise web.HTTPServiceUnavailable(text="SQLite unavailable for writes") from None

        return web.Response(text="ready\n")

    async def metrics(_request: web.Request) -> web.Response:
        """Refresh durable gauges and expose Prometheus metrics."""

        state = await store.metrics()
        PENDING.set(state["pending"])
        ACTIVE.set(state["active"])
        OLDEST_PENDING.set(state["oldest_pending"])
        CANARY_ENABLED.set(canary_enabled)
        CANARY_SEEN.set(state["last_seen"] if canary_enabled else 0)
        CANARY_HEALTHY.set(canary_enabled and time.time() - state["last_seen"] <= canary_timeout)
        CANARY_TIMEOUT.set(canary_timeout if canary_enabled else 0)
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    app.router.add_post("/webhook/grafana", webhook)

    if canary_enabled:
        app.router.add_post("/webhook/canary", webhook)

    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    app.router.add_get("/metrics", metrics)

    return app


async def deliver_pending(delivery: Delivery) -> None:
    """
    Continuously process due incident updates.

    Args:
        delivery: Worker owning the queue and Telegram transport.
    """

    while True:
        if not await delivery.once(time.time()):
            await asyncio.sleep(1)


async def maintain_store(store: Store, *, canary_enabled: bool, max_silence: float, retention: float) -> None:
    """
    Expire closed incident history and optionally check heartbeat deadlines.

    Args:
        store: Persistent incident and heartbeat state.
        canary_enabled: Whether heartbeat silence creates watchdog incidents.
        max_silence: Maximum heartbeat silence in seconds.
        retention: Age limit for delivered resolved incidents in seconds.
    """

    cleanup_at = 0.0

    while True:
        now = time.time()

        if canary_enabled:
            await store.check_canary(now, max_silence)

        if now >= cleanup_at:
            await store.cleanup(now - retention)
            cleanup_at = now + 3600

        await asyncio.sleep(min(5, max_silence) if canary_enabled else 3600)


async def application() -> web.Application:
    """
    Create production transports, persistent state, and worker lifecycle.

    Returns:
        The production aiohttp application.
    """

    settings = Settings()
    store = await Store.open(settings.state_db, time.time())

    if not settings.canary_enabled:
        await store.disable_canary(time.time())

    app = create_app(store, canary_timeout=settings.canary_timeout_seconds, canary_enabled=settings.canary_enabled)

    telegram_request = HTTPXRequest(connection_pool_size=3, read_timeout=40, connect_timeout=10)
    telegram = Bot(
        settings.bot_token.get_secret_value(), request=telegram_request, get_updates_request=telegram_request
    )

    session = aiohttp.ClientSession()
    render = Renderer(session, str(settings.grafana_url), settings.grafana_token.get_secret_value())
    templates = MessageTemplates(settings.templates_dir, default=settings.message_template)

    delivery = Delivery(
        store,
        telegram,
        settings.chat_id.get_secret_value(),
        render,
        DeliveryOptions(templates=templates, reminder_interval=settings.reminder_interval),
    )
    callbacks = Callbacks(store, telegram, delivery.chat_id)

    async def lifecycle(app: web.Application) -> AsyncIterator[None]:
        """Start workers and release every owned transport during shutdown."""

        # Initialize the HTTP transport without requiring a successful getMe at startup.
        await telegram_request.initialize()

        app[TASKS].extend(
            [
                asyncio.create_task(deliver_pending(delivery)),
                asyncio.create_task(callbacks.run()),
                asyncio.create_task(
                    maintain_store(
                        store,
                        canary_enabled=settings.canary_enabled,
                        max_silence=settings.canary_timeout_seconds,
                        retention=settings.retention_period,
                    )
                ),
            ]
        )

        try:
            yield

        finally:
            for task in app[TASKS]:
                task.cancel()

            for task in app[TASKS]:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            await telegram_request.shutdown()
            await session.close()
            await store.close()

    app.cleanup_ctx.append(lifecycle)

    return app


def main() -> None:
    """Configure credential-safe logging and run the HTTP service."""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    for logger in ("httpx", "httpcore", "telegram"):
        logging.getLogger(logger).setLevel(logging.WARNING)

    # Accept traffic from other containers; Compose publishes only on loopback.
    web.run_app(application(), host="0.0.0.0", port=8080, access_log=None)  # noqa: S104


if __name__ == "__main__":
    main()
