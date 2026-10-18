FROM python:3.15-alpine AS base

ENV PTB_TIMEDELTA=true \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

LABEL org.opencontainers.image.title="grafana-telegram-relay" \
    org.opencontainers.image.description="Persistent Grafana alerts in Telegram" \
    org.opencontainers.image.documentation="https://github.com/insan3d/grafana-telegram-relay#readme" \
    org.opencontainers.image.licenses="Apache-2.0" \
    org.opencontainers.image.source="https://github.com/insan3d/grafana-telegram-relay" \
    org.opencontainers.image.version="0.1.0" \
    org.opencontainers.image.url="https://hub.docker.com/r/apozlevich/grafana-telegram-relay" \
    org.opencontainers.image.authors="Alexander Pozlevich"

FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_CACHE_DIR=/root/.cache/uv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --locked --no-dev --no-emit-project --output-file requirements.txt \
 && uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python --require-hashes --requirement requirements.txt

COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /dist \
 && uv pip install --python /opt/venv/bin/python --no-deps /dist/*.whl \
 && uv pip check --python /opt/venv/bin/python

FROM base AS runtime

RUN addgroup -g 1000 relay \
 && adduser -D -u 1000 -G relay relay \
 && mkdir /data \
 && chown relay:relay /data

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /data

USER 1000:1000

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=5 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/readyz', timeout=2).close()"]

CMD ["grafana-telegram-relay"]
