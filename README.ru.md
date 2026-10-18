# grafana-telegram-relay

[English](README.md) · [Apache-2.0](LICENSE)

[GitHub](https://github.com/insan3d/grafana-telegram-relay) ·
[Docker Hub](https://hub.docker.com/r/apozlevich/grafana-telegram-relay)

Доставка алертов Grafana в Telegram: одно постоянное сообщение на инцидент. Изменения, ACK и восстановление редактируют
его; новое срабатывание создаёт новое сообщение. SQLite сохраняет очередь доставки, ID сообщений, подтверждения, offset
long polling и состояние canary после перезапуска.

Python 3.15, aiohttp, асинхронный Telegram API-клиент и sandboxed Jinja2. Встроенные шаблоны входят в wheel.
Runtime-образ на Alpine содержит установленный wheel и зависимости в venv; uv используется только на этапе сборки.

## 1. Подготовить Telegram и Grafana

Создайте бота через [BotFather](https://t.me/BotFather), добавьте его в группу и разрешите отправку сообщений и
фотографий. До запуска сервиса отправьте в группе `/start@имя_бота` и определите числовой chat ID через Telegram
[`getUpdates`](https://core.telegram.org/bots/api#getupdates). Администратор-бот может увидеть событие группы. Сам relay
обрабатывает только нажатия кнопок; команды `/start` он не реализует. Другой потребитель updates с тем же токеном не
должен работать одновременно с relay.

В Grafana создайте service account с ролью Viewer и токеном. Для скриншотов настройте Grafana image rendering. Токен
передаётся только настроенному серверу Grafana. Алерты без связанной панели отправляются текстом; ошибка рендера также
не блокирует доставку текста.

Из корня проекта сохраните секреты, не помещая их значения в shell history:

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

Каталог закрыт, а права файлов позволяют процессу с UID 1000 читать Compose secrets. Секреты, базы и локальные окружения
исключены из Git и Docker build context.

## 2. Запустить relay

Установите Docker с Compose. Укажите адрес Grafana, доступный **из контейнера**, включая subpath:

```bash
export GRAFANA_URL='https://grafana.example.com/grafana'
docker compose up --detach --build --wait
curl --fail http://127.0.0.1:8088/readyz
```

Готовый [compose.yaml](compose.yaml) публикует HTTP на `127.0.0.1:8088`, подключает secrets и сохраняет SQLite в named
volume. Процесс работает с UID/GID 1000. `docker compose down` сохраняет данные. После ротации секретов пересоздайте
контейнер. Webhook не имеет встроенной авторизации: подключайте его через доверенную сеть или reverse proxy с
авторизацией.

Для локальной разработки установите [uv](https://docs.astral.sh/uv/getting-started/installation/), затем:

```bash
make setup
export BOT_TOKEN_FILE="$PWD/.secrets/bot-token"
export CHAT_ID_FILE="$PWD/.secrets/chat-id"
export GRAFANA_TOKEN_FILE="$PWD/.secrets/grafana-token"
install -d .local-data
export STATE_DB="$PWD/.local-data/state.db"
uv run --locked grafana-telegram-relay
```

Приложение слушает `0.0.0.0:8080`; Compose ограничивает опубликованный адрес хоста loopback-интерфейсом.

## 3. Подключить Grafana и canary

Создайте webhook contact point с адресом `/webhook/grafana`, доступным из Grafana. Задайте **Max Alerts = 0**, оставьте
уведомления о восстановлении включёнными. Направьте правила в этот contact point через notification policies. Grafana
может группировать webhook, но relay создаёт отдельное Telegram-сообщение на каждое сочетание `fingerprint + startsAt`.

Полный [пример provisioning Grafana](examples/grafana-alerting.yaml) содержит обычный и canary contact points, постоянно
firing-правило `GrafanaCanary` на константном выражении и notification policy. Замените URL вебхуков на доступный
Grafana адрес relay. `http://relay:8080` работает в общей Docker-сети. Перед загрузкой объедините политику с имеющимся
деревом: пример задаёт корневую политику организации. Подключите файл в `/etc/grafana/provisioning/alerting/` и
перезапустите Grafana.

Canary постоянно firing с labels `alertname=GrafanaCanary`, `canary=true`. Направляйте его раз в минуту в
`/webhook/canary`, без начальной задержки и уведомлений о восстановлении. Heartbeat не создаёт сообщений в Telegram.
После 180 секунд тишины независимый watchdog создаёт `GrafanaCanaryMissing`; восстановление редактирует то же сообщение.
Настройте маршрут сразу: watchdog включён с запуска и сохраняет отсчёт при перезапуске.

Для скриншотов свяжите настоящее правило с дашбордом и панелью. Relay использует аннотации `__dashboardUid__` и
`__panelId__` либо `panelURL` из webhook. Переменные панели `instance` и `project` получают labels `instance` и
`compose_project`, если они есть.

## 4. Проверить доставку, ACK и восстановление

Пример требует POSIX shell, `curl` и `date`. Он отправляет обычный webhook и настоящее сообщение в группу:

```bash
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
./examples/notify.sh firing "$started_at"
```

Нажмите **Acknowledge**. В сообщении появится `Acknowledged by @username` либо полное имя пользователя, а кнопка ACK
исчезнет. Первый автор подтверждения сохраняется при обновлениях, восстановлении и перезапуске. Затем:

```bash
./examples/notify.sh resolved "$started_at"
curl --fail http://127.0.0.1:8088/metrics
```

Первое сообщение станет resolved. Для следующего инцидента получите новый `started_at`. Через `RELAY_URL` можно выбрать
другой адрес relay. У минимального примера нет связанной панели, поэтому сообщение текстовое.

ACK занимает отдельную строку клавиатуры во всю ширину. Ниже идут доступные ссылки: **Runbook** из аннотации
`runbook_url`, **Grafana** из `dashboardURL` либо дашборда в `panelURL`, **Silence** из `silenceURL`. Silence открывает
форму Grafana; relay сам не создаёт silence. Telegram отвергает URL-кнопки с `localhost`, поэтому relay подставляет
`127.0.0.1`. Для переходов с других устройств задайте доступный им публичный адрес Grafana.

## Конфигурация и HTTP API

| Переменная | Значение по умолчанию / назначение |
| --- | --- |
| `BOT_TOKEN_FILE` | Обязательно: путь к файлу токена Telegram |
| `CHAT_ID_FILE` | Обязательно: путь к файлу chat ID |
| `GRAFANA_TOKEN_FILE` | Обязательно: путь к файлу Viewer-токена Grafana |
| `GRAFANA_URL` | Обязательно: базовый адрес Grafana, доступный процессу |
| `STATE_DB` | `/data/state.db`; каталог должен допускать запись |
| `TEMPLATES_DIR` | `/etc/grafana-telegram-relay/templates`; каталог пользовательских шаблонов |
| `MESSAGE_TEMPLATE` | `default.txt.j2`; общий шаблон |
| `CANARY_TIMEOUT_SECONDS` | `180`; положительный таймаут heartbeat |
| `RETENTION_PERIOD` | `30d`; положительная длительность через humanfriendly: `12h`, `90m`, `1.5h` |

Настройки проверяются до открытия SQLite и HTTP-клиентов. Файлы секретов должны существовать и содержать непустые
значения; `GRAFANA_URL` должен быть абсолютным HTTP- или HTTPS-адресом.

| Endpoint | Поведение |
| --- | --- |
| `POST /webhook/grafana` | Webhook Grafana: 202 после сохранения, 400 при неверном/усечённом payload |
| `POST /webhook/canary` | Только firing-heartbeat canary |
| `GET /healthz` | Живость HTTP-процесса |
| `GET /readyz` | Записываемая SQLite и работающие workers; 503 при локальной неисправности |
| `GET /metrics` | Метрики Prometheus |

Ready остаётся положительным при недоступности Grafana и Telegram, пока webhook можно сохранить в очередь. Retry и общие
паузы по rate limit Telegram сохраняются в SQLite. Идентичные payload дедуплицируются. Доставленные закрытые инциденты
хранятся в пределах retention; активные и ожидающие доставки не удаляются.

Запускайте **одну реплику на токен бота и базу**. У отправки Telegram нет idempotency key: авария между отправкой и
сохранением ID либо неоднозначный сетевой таймаут могут привести к дублю. Canary проверяет цепочку уведомлений Grafana;
для обнаружения падения relay, его хоста или самого Telegram нужна внешняя проверка.

## Шаблоны сообщений

Встроенные [шаблоны](src/grafana_telegram_relay/templates/) содержат обычный текст и компактный HTML. Подключите свой
каталог в `/etc/grafana-telegram-relay/templates` для добавления или переопределения шаблонов. Файлы, includes и macros
читаются при каждом изменившемся алерте; правка файла сама по себе не запускает доставку. Общий шаблон выбирается через
`MESSAGE_TEMPLATE`, шаблон правила — аннотацией `telegram_template=compact.html.j2`.

```jinja2
{{ status|upper }} · {{ labels.alertname }}
{{ annotations.get('summary', '') }}
{% for key, value in values|dictsort %}
{{ key }}: {{ value }}
{% endfor %}
```

Контекст: `alert`, `status`, `labels`, `annotations`, `values`, `starts_at`, `ends_at`, `duration_seconds`, `now`,
`dashboard_url`, `panel_url`, `generator_url`, `silence_url`, `fingerprint`, `acknowledgement`. Подтверждение — `None`
либо объект с `name`, `user_id`, `at` и внутренним `version`; подпись ACK добавляется автоматически.

Для `*.html.j2` включён Telegram HTML с экранированием во всём дереве includes/macros; остальные шаблоны текстовые.
Неопределённые переменные, отсутствующие/сломанные шаблоны и некорректный HTML вызывают fallback к тексту. Лимиты — 4096
UTF-16 units для текста и 1024 для подписей фото. Длинный HTML переводится в текст перед обрезкой.

## Метрики и дашборд

Собирайте `/metrics` через Prometheus, VictoriaMetrics или совместимый коллектор. Импортируйте
[dashboards/grafana-telegram-relay.json](dashboards/grafana-telegram-relay.json) в Grafana и выберите **Metrics → Job →
Instance**. Фиксированные UID датасорса, имя job и Compose labels не нужны. Гистограммы и счётчики с labels появляются
после первого соответствующего события.

```yaml
scrape_configs:
  - job_name: grafana-telegram-relay
    static_configs:
      - targets: [relay:8080]
```

Дашборд показывает HTTP-запросы и задержки, принятые webhook, попытки и ошибки доставки/рендера, ошибки шаблонов,
результаты ACK, ошибки callbacks, постоянную очередь и активные инциденты, возраст/здоровье/таймаут canary и
CPU/RSS/uptime процесса. У метрик префикс `telegram_bot_`, дополнительно доступны стандартные Python/process-метрики.
Labels не содержат chat ID, токенов, fingerprint и текста сообщений. Probes и scrape исключены из HTTP-счётчиков.

## Разработка и дистрибутивы

Нужны GNU Make и uv. Python 3.15 и нативные бинарники линтеров устанавливаются через зафиксированные dev dependencies;
prek управляет Go toolchain для checkmake. Для линтеров Docker не требуется.

```bash
make setup
make hooks
make check
make build
make image
```

Все цели перечислены в `make help`. `make format` форматирует Python/TOML/Markdown; `make coverage` показывает покрытие.
`make lint` включает отслеживаемые и новые файлы. Git hooks запускают линтеры перед коммитом и offline-тесты перед push.
Хуки не отправляют сообщения в Telegram. `make clean` удаляет артефакты, сохраняя secrets и постоянные данные.

Проверки: Ruff ALL, строгий mypy, импорты зависимостей и актуальность lock, метаданные пакета, валидация/формат TOML,
линт YAML, валидация JSON, формат JSON, rumdl ALL, нативный hadolint, checkmake, ShellCheck,
поиск встроенных секретов и стандартная файловая гигиена. Стиль Python описан в [AGENTS.md](AGENTS.md).

`make build` создаёт wheel и sdist в `dist/` и проверяет их через Twine. Для установки wheel в окружение Python 3.15
используйте `pip install dist/grafana_telegram_relay-*.whl`, затем `grafana-telegram-relay` с теми же переменными
окружения. Встроенные шаблоны и лицензии входят в wheel. Docker-сборка устанавливает runtime-зависимости из lock с
проверкой хешей, устанавливает wheel и копирует `/opt/venv` в финальный слой.

## Лицензия

[Apache License 2.0](LICENSE), copyright 2026 Alexander Pozlevich. Лицензия проекта распространяется на код,
документацию, примеры и дашборды; зависимости сохраняют собственные лицензии.
