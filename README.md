# grafana-telegram-relay

[Русский](README.ru.md) · [Apache-2.0](LICENSE)

[GitHub](https://github.com/insan3d/grafana-telegram-relay) ·
[Docker Hub](https://hub.docker.com/r/apozlevich/grafana-telegram-relay)

Grafana alert notifications in Telegram with one persistent message per alert incident. Firing updates, acknowledgements
and recovery edit that message; recovery also sends a brief reply with mentions. A new firing episode creates a new
incident message. SQLite preserves the delivery queue, Telegram message IDs, recovery replies, acknowledgements, polling
offset and canary state across restarts.

Python 3.15, aiohttp, an asynchronous Telegram API client and sandboxed Jinja2 templates. The wheel includes the default
templates. The Alpine runtime image contains the installed wheel and its dependencies in a virtual environment; uv is
used only during the build.

## 1. Prepare Telegram and Grafana

Create a bot with [BotFather](https://t.me/BotFather), add it to your group and allow it to send messages and photos.
Before starting the relay, send `/start@your_bot` in the group and obtain its numeric chat ID through Telegram
[`getUpdates`](https://core.telegram.org/bots/api#getupdates). A bot administrator can see the group event. The relay
itself processes button callbacks only; it does not implement `/start` commands. Do not run another update consumer with
the same token alongside the relay.

Create a Grafana service account with the Viewer role and generate a token. Enable Grafana image rendering for
screenshots. The token is sent only to the configured Grafana URL. Alerts without an associated panel are sent as text;
a rendering failure also falls back to text.

From this project's root, save secrets without putting their values into shell history:

```bash
install -d -m 700 .secrets
read -rsp 'Telegram bot token: ' relay_token
printf '\n'
printf '%s' "$relay_token" > .secrets/bot-token
unset relay_token
read -rp 'Telegram group chat ID: ' relay_chat
printf '%s' "$relay_chat" > .secrets/chat-id
unset relay_chat
read -rsp 'Grafana Viewer token: ' relay_grafana_token
printf '\n'
printf '%s' "$relay_grafana_token" > .secrets/grafana-token
unset relay_grafana_token
chmod 644 .secrets/bot-token .secrets/chat-id .secrets/grafana-token
```

The directory is private; readable files allow the container's UID 1000 to use Compose secrets. Secrets, databases and
local environments are excluded from Git and the Docker build context.

## 2. Start the relay

Install Docker with Compose. Set a Grafana address reachable **from the container**, including any subpath:

```bash
export GRAFANA_URL='https://grafana.example.com/grafana'
docker compose up --detach --build --wait
curl --fail http://127.0.0.1:8088/readyz
```

The included [compose.yaml](compose.yaml) publishes HTTP on `127.0.0.1:8088`, mounts secrets and persists SQLite in a
named volume. Its image runs as UID/GID 1000. `docker compose down` preserves data. Recreate the container after
rotating secrets. The webhook has no authentication: expose it through a trusted network or an authenticated reverse
proxy.

For a local development run, install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
make setup
export BOT_TOKEN_FILE="$PWD/.secrets/bot-token"
export CHAT_ID_FILE="$PWD/.secrets/chat-id"
export GRAFANA_TOKEN_FILE="$PWD/.secrets/grafana-token"
install -d .local-data
export STATE_DB="$PWD/.local-data/state.db"
uv run --locked grafana-telegram-relay
```

The application listens on `0.0.0.0:8080`; Compose restricts the published host address to loopback.

## 3. Connect Grafana

Create a webhook contact point pointing at `/webhook/grafana` using an address Grafana can reach. Set **Max Alerts = 0**
and keep resolved notifications enabled. Route your alert rules to this contact point through notification policies.
Grafana may group a webhook, but the relay creates one Telegram message per alert's `fingerprint + startsAt`.

The [Grafana provisioning example](examples/grafana-alerting.yaml) defines the ordinary contact point and notification
policy. Set its webhook URL to the relay's reachable address. `http://relay:8080` works when Grafana and the relay share
a Docker network. Merge its policy with an existing notification tree before loading it: the file defines the
organization's root policy. Mount the result under `/etc/grafana/provisioning/alerting/` and restart Grafana.

The canary is optional and disabled by default. To enable it, set `CANARY_ENABLED=true` (also supported by Compose) and
use the [canary provisioning example](examples/grafana-alerting-canary.yaml) instead of the ordinary example. It adds a
continuously firing `GrafanaCanary` rule with labels `alertname=GrafanaCanary` and `canary=true`, plus a separate
contact point. Send heartbeats every minute to `/webhook/canary`, with no initial wait and resolved notifications
disabled. Heartbeats create no Telegram messages. After 180 seconds without one, the independent watchdog creates
`GrafanaCanaryMissing`; recovery edits the same message. The enabled watchdog preserves its deadline across restarts.

With `CANARY_ENABLED=false`, `/webhook/canary` returns 404 and heartbeats are not checked. Startup discards unfinished
internal canary incidents and resets the heartbeat deadline; delivered closed history is retained. Messages already
sent to Telegram remain in the chat. Retention cleanup for ordinary incidents runs in either mode.

To disable a previously provisioned canary, delete its Grafana rule before removing its notification route or switching
to the ordinary example. Omitting a rule from provisioning does not delete it; a remaining canary rule can reach the
ordinary Telegram contact point. Remove the canary `groups` entry and add this top-level entry to your provisioning,
then reload alerting provisioning or restart Grafana:

```yaml
deleteRules:
  - orgId: 1
    uid: relay-canary
```

Use your rule's UID and organization ID if customized. Remove this deletion entry before enabling the canary again.

Associate a real alert rule with a dashboard/panel to receive screenshots. The relay accepts `__dashboardUid__` and
`__panelId__` annotations or the webhook's `panelURL`. Panel variables `instance` and `project` receive the alert's
`instance` and `compose_project` labels when present.

## 4. Verify delivery, ACK and recovery

The example requires a POSIX shell, `curl` and `date`. It posts a normal webhook and sends a real group message:

```bash
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
./examples/notify.sh firing "$started_at"
```

Press **Acknowledge**. The message receives `Acknowledged by @username`, or the user's full name, and the ACK button
disappears. The first acknowledger is retained through updates, recovery and restarts. Then:

```bash
./examples/notify.sh resolved "$started_at"
curl --fail http://127.0.0.1:8088/metrics
```

The original message becomes resolved, and the relay sends a short `🟢 Решено` reply with the rule's `mention` label,
without screenshots, descriptions or buttons. Repeated webhooks do not send another reply; failed replies remain queued
across restarts. If the first delivered notification is already resolved, only the full incident message is sent.
Use a new `started_at` for the next incident. Set `RELAY_URL` when testing another relay address. This minimal example
has no panel association and is sent as text.

ACK occupies a separate full-width keyboard row. Available links appear below it: **Runbook** from the `runbook_url`
annotation, **Grafana** from `dashboardURL` or the dashboard behind `panelURL`, and **Silence** from `silenceURL`.
Silence opens Grafana's form; the relay does not create silences. Telegram rejects `localhost` URL buttons, so the relay
uses `127.0.0.1` for those links. Configure Grafana's public URL for access from other devices.

## Configuration and HTTP API

| Variable | Default / purpose |
| --- | --- |
| `BOT_TOKEN_FILE` | Required: Telegram token file |
| `CHAT_ID_FILE` | Required: target chat ID file |
| `GRAFANA_TOKEN_FILE` | Required: Grafana Viewer token file |
| `GRAFANA_URL` | Required: Grafana base URL reachable from the process |
| `STATE_DB` | `/data/state.db`; directory must be writable |
| `TEMPLATES_DIR` | `/etc/grafana-telegram-relay/templates`; optional template overrides |
| `MESSAGE_TEMPLATE` | `default.html.j2`; global template name |
| `REMINDER_INTERVAL` | `0s` disables repeats; e.g. `5m` repeats firing alerts until ACK |
| `CANARY_ENABLED` | `false`; enable canary heartbeats and watchdog |
| `CANARY_TIMEOUT_SECONDS` | `180`; positive heartbeat timeout |
| `RETENTION_PERIOD` | `30d`; positive duration parsed by humanfriendly, e.g. `12h`, `90m`, `1.5h` |

Configuration is validated before SQLite and HTTP clients are opened. Secret files must exist and contain nonempty
values; `GRAFANA_URL` must be an absolute HTTP or HTTPS URL.

| Endpoint | Behavior |
| --- | --- |
| `POST /webhook/grafana` | Grafana webhook; 202 after persistence; 400 for invalid/truncated payloads |
| `POST /webhook/canary` | Firing canary heartbeats only; 404 when disabled |
| `GET /healthz` | HTTP process liveness |
| `GET /readyz` | Writable SQLite and running workers; 503 on local failure |
| `GET /metrics` | Prometheus metrics |

Readiness remains healthy during Grafana or Telegram outages while webhooks can still be queued. Delivery retries
persist in SQLite, including Telegram rate-limit pauses. Identical payloads are deduplicated. Delivered closed incidents
are retained for the configured period; active and pending incidents are not pruned.

Run **one replica per bot token and database**. Telegram has no send idempotency key: a crash between sending and saving
the message ID, or an ambiguous network timeout, can cause a duplicate. The canary monitors the Grafana notification
chain; an external check is needed to detect failure of the relay, its host or Telegram itself. The same send/commit
limitation applies to recovery replies and reminders.

## Repeated notifications until ACK

Set `REMINDER_INTERVAL=5m` to send a new copy every five minutes while an incident is firing and unacknowledged.
The default `0s` disables repeats. The supplied Compose file forwards this variable; recreate the container after
changing it. Repeats do not require another Grafana webhook and preserve the original text, mentions and screenshot,
with an **Acknowledge** button on every copy. Screenshots are copied without rendering the panel again.

Acknowledging the original or any copy acknowledges the whole incident. The relay deletes all tracked copies and
updates the original with the operator's name. Recovery also stops repeats and removes copies, retaining the original
and its recovery reply. Schedules, copy IDs and deletion retries survive restarts; cleanup continues when repeats are
disabled and after incident history expires. After downtime the relay sends one due reminder, without replaying every
missed interval. `telegram_bot_reminders_total{action="send|delete",result="success|failure"}` records these operations.

[Telegram limits message deletion to 48 hours](https://core.telegram.org/bots/api#deletemessage), so the relay removes
day-old copies even before ACK while continuing to send new reminders. A bot outage longer than 48 hours or missing
Telegram permissions can prevent cleanup of old copies; failed deletions remain queued. The original is never removed
by reminder cleanup.

## Message templates

The default [template](src/grafana_telegram_relay/templates/default.html.j2) uses Telegram HTML with a bold title and
summary, separate description and label sections, inline code for label values and timestamps, and a code block for
query values.
Packaged templates also include `compact.html.j2` and plain `default.txt.j2`. Mount your own directory at
`/etc/grafana-telegram-relay/templates` to override or add templates. Files, includes and macros are reread on each
changed alert; a file edit alone does not trigger delivery. Select a template globally with `MESSAGE_TEMPLATE` or per
rule with annotation `telegram_template=compact.html.j2`.

Add a `mention` label to a Grafana alert rule to tag Telegram users without a custom template:

```yaml
labels:
  mention: "@alice @bob"
```

All built-in templates display this value once below the title, outside code formatting, for both firing and resolved
alerts, including photo captions. Use usernames with `@`, separated by spaces. The label is omitted from the technical
label list; HTML in its value is escaped. Custom templates can display it with `{{ labels.get('mention', '') }}`.

```jinja2
{{ status|upper }} · {{ labels.alertname }}
{{ annotations.get('summary', '') }}
{% for key, value in values|dictsort %}
{{ key }}: {{ value }}
{% endfor %}
```

Context: `alert`, `status`, `labels`, `annotations`, `values`, `starts_at`, `ends_at`, `duration_seconds`, `now`,
`dashboard_url`, `panel_url`, `generator_url`, `silence_url`, `fingerprint`, `acknowledgement`. Acknowledgement is
`None` or an object with `name`, `user_id`, `at` and internal `version`; the ACK footer is appended automatically.

`*.html.j2` enables Telegram HTML with automatic escaping throughout includes/macros; other templates are plain text.
Undefined variables, broken/missing templates and invalid HTML fall back to plain text. Limits are 4096 UTF-16 units for
text and 1024 for photo captions. Long HTML is converted to plain text before clipping.

## Metrics and dashboard

Scrape `/metrics` with Prometheus, VictoriaMetrics or another compatible collector. Import
[dashboards/grafana-telegram-relay.json](dashboards/grafana-telegram-relay.json) in Grafana and select **Metrics → Job →
Instance**. No fixed datasource UID, job name or Compose labels are required. Histograms and labelled counters appear
after the first corresponding event.

```yaml
scrape_configs:
  - job_name: grafana-telegram-relay
    static_configs:
      - targets: [relay:8080]
```

The dashboard covers HTTP requests and latency, accepted webhooks, delivery/render attempts and failures, template
failures, ACK results, callback failures, persistent queue and active incidents, optional canary age/health/timeout, and
process CPU/RSS/uptime. Metrics use the `telegram_bot_` prefix plus standard Python/process metrics. Labels never
include chat IDs, tokens, fingerprints or message text. Probes and scrapes are excluded from HTTP request counters.

`telegram_bot_canary_enabled` reports whether the canary is enabled. Other canary gauges are zero while disabled;
the dashboard shows `DISABLED` and hides heartbeat age and timeout.

## Development and distributions

Install GNU Make and uv. Python 3.15 and native linter executables are installed through the locked dev dependencies;
prek manages the Go toolchain for checkmake. Linters do not require Docker.

```bash
make setup
make hooks
make check
make build
make image
```

`make help` lists targets. `make format` formats Python/TOML/Markdown; `make coverage` reports coverage. `make lint`
includes tracked and untracked files. Git hooks run linters at commit time and offline tests before push. No hook posts
to Telegram. `make clean` removes generated artifacts and keeps secrets and persistent data.

Release versions follow `MAJOR.MINOR.PATCH`; the current release is `0.2.1`. Update the version with
`uv version --bump patch` and keep the Dockerfile's default OCI version label in sync. `make publish` runs checks and
builds the
distributions, then builds and pushes `apozlevich/grafana-telegram-relay:0.2.1` and `:latest` using the version from
`pyproject.toml`. The image's OCI version label uses the same value. Override `REPOSITORY` for another registry.
Release tags contain versions; `latest` follows the most recently published release.

Checks include Ruff ALL, strict mypy, dependency imports and lock consistency, package metadata, TOML validation and
format, YAML lint, JSON validation, JSON formatting, rumdl ALL, native hadolint, checkmake, ShellCheck,
embedded-secret detection and standard file hygiene. Python code style is documented in
[AGENTS.md](AGENTS.md).

`make build` creates a wheel and source distribution in `dist/` and validates both with Twine. Install the wheel into a
Python 3.15 environment with `pip install dist/grafana_telegram_relay-*.whl` and run `grafana-telegram-relay` with the
same environment variables. Built-in templates and license notices are part of the wheel. The Docker build installs
locked runtime dependencies with hash verification, installs the wheel, then copies `/opt/venv` into the runtime stage.

## License

[Apache License 2.0](LICENSE), copyright 2026 Alexander Pozlevich. The project license covers code, documentation,
examples and dashboards; dependencies retain their respective licenses.
