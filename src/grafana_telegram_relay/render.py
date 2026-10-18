"""
Render associated dashboard panels through the configured Grafana server.

Webhook URLs provide panel identifiers only. Authentication is sent exclusively
to the configured server, and screenshots are bounded by Telegram's photo limit.
"""

import re
from datetime import UTC, datetime
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import aiohttp

lazy from .models import Alert


class Renderer:
    """Fetch authenticated PNG screenshots for alerts associated with panels."""

    def __init__(self, session: aiohttp.ClientSession, grafana_url: str, token: str) -> None:
        """
        Initialize a renderer with a caller-owned HTTP session.

        Args:
            session: Shared asynchronous HTTP session.
            grafana_url: Configured Grafana base URL, including its subpath.
            token: Viewer service-account token used for screenshot requests.
        """

        self.session = session
        self.grafana_url = grafana_url.rstrip("/")
        self.token = token

    async def __call__(self, item: Alert) -> bytes | None:
        """
        Render the associated panel or skip an unassociated alert.

        Args:
            item: Validated alert with panel metadata and template variables.

        Returns:
            Complete PNG bytes, or None when no valid panel is associated.

        Raises:
            aiohttp.ClientError: Grafana rejects or cannot serve the request.
            TimeoutError: Rendering exceeds the request deadline.
            ValueError: The response is not a PNG or exceeds the photo limit.
        """

        annotations = item.annotations
        uid = annotations.get("__dashboardUid__")
        panel = annotations.get("__panelId__")

        if not uid or not panel:
            url = urlsplit(item.panel_url or "")
            match = re.search(r"/d/([a-zA-Z0-9_-]+)(?:/|$)", url.path)
            uid = match[1] if match else None
            panel = parse_qs(url.query).get("viewPanel", [""])[0].removeprefix("panel-")

        if not uid or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", uid):
            return None

        if not panel or not str(panel).isdigit():
            return None

        start = datetime.fromisoformat(item.starts_at).timestamp()
        end = datetime.now(UTC).timestamp()

        if item.status == "resolved" and item.ends_at:
            end = datetime.fromisoformat(item.ends_at).timestamp()

        params = {
            "panelId": str(panel),
            "orgId": "1",
            "width": "1200",
            "height": "600",
            "from": str(int((start - 900) * 1000)),
            "to": str(int(end * 1000)),
            "tz": "UTC",
            "timeout": "20",
            "var-project": item.labels.get("compose_project", "observability"),
            "var-instance": item.labels.get("instance", "$__all"),
        }

        async with self.session.get(
            f"{self.grafana_url}/render/d-solo/{uid}/_",
            params=params,
            headers={"Authorization": "Bearer " + self.token},
            timeout=aiohttp.ClientTimeout(total=25),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()

            if response.status != HTTPStatus.OK or response.content_type != "image/png":
                msg = "Grafana did not return a PNG"
                raise ValueError(msg)

            image = bytearray()

            async for chunk in response.content.iter_chunked(65536):
                image.extend(chunk)

                if len(image) > 10 * 1024 * 1024:
                    msg = "Grafana screenshot exceeds Telegram photo limit"
                    raise ValueError(msg)

            return bytes(image)
