"""
Declare bounded Prometheus metrics for HTTP, delivery, and watchdog state.

Counters and histograms describe the current process lifetime. Queue and canary
gauges are refreshed from durable SQLite state during each scrape.
"""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "telegram_bot_http_requests_total",
    "HTTP requests excluding probes and scrapes",
    ["route", "method", "status"],
)
HTTP_DURATION = Histogram(
    "telegram_bot_http_request_duration_seconds",
    "HTTP request duration excluding probes and scrapes",
    ["route", "method"],
)
WEBHOOK_ALERTS = Counter(
    "telegram_bot_webhook_alerts_total",
    "Accepted alerts including duplicate notifications",
    ["endpoint"],
)
DELIVERIES = Counter("telegram_bot_deliveries_total", "Telegram delivery attempts", ["result"])
DELIVERY_DURATION = Histogram(
    "telegram_bot_delivery_duration_seconds",
    "Delivery attempt duration including rendering",
    ["result"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120),
)
RENDER_FAILURES = Counter("telegram_bot_render_failures_total", "Failed Grafana screenshots")
TEMPLATE_FAILURES = Counter("telegram_bot_template_failures_total", "Message template failures")
RENDER_DURATION = Histogram(
    "telegram_bot_render_duration_seconds",
    "Grafana screenshot attempts including skipped panels",
    ["result"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
)
PENDING = Gauge("telegram_bot_pending_incidents", "Incidents awaiting Telegram delivery")
ACTIVE = Gauge("telegram_bot_active_incidents", "Stored incidents currently firing")
OLDEST_PENDING = Gauge(
    "telegram_bot_oldest_pending_update_timestamp_seconds",
    "Timestamp of the oldest undelivered update, zero when the queue is empty",
)
CANARY_ENABLED = Gauge("telegram_bot_canary_enabled", "Whether the optional canary watchdog is enabled")
CANARY_SEEN = Gauge(
    "telegram_bot_canary_last_seen_seconds", "Last valid canary heartbeat timestamp, zero when disabled"
)
CANARY_HEALTHY = Gauge("telegram_bot_canary_healthy", "Whether the last canary heartbeat is within its timeout")
CANARY_TIMEOUT = Gauge("telegram_bot_canary_timeout_seconds", "Configured canary heartbeat timeout, zero when disabled")

ACKNOWLEDGEMENTS = Counter("telegram_bot_acknowledgements_total", "Incident acknowledgement callbacks", ["result"])
CALLBACK_FAILURES = Counter("telegram_bot_callback_failures_total", "Failed Telegram polls or callback answers")
REMINDERS = Counter("telegram_bot_reminders_total", "Reminder copy and deletion attempts", ["action", "result"])
