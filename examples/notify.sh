#!/usr/bin/env sh
set -eu

status=${1:?Usage: notify.sh firing|resolved STARTS_AT}
starts_at=${2:?Pass the same RFC3339 STARTS_AT for firing and resolved}

case "${status}" in
  firing) ends_at='0001-01-01T00:00:00Z' ;;
  resolved) ends_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) ;;
  *) printf '%s\n' 'Status must be firing or resolved' >&2; exit 2 ;;
esac

curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @- "${RELAY_URL:-http://127.0.0.1:8088}/webhook/grafana" <<EOF
{
  "receiver": "telegram",
  "status": "${status}",
  "groupKey": "example-relay-smoke",
  "groupLabels": {"alertname": "RelaySmokeTest"},
  "alerts": [{
    "status": "${status}",
    "fingerprint": "relay-smoke-test",
    "startsAt": "${starts_at}",
    "endsAt": "${ends_at}",
    "labels": {"alertname": "RelaySmokeTest", "instance": "demo", "severity": "test"},
    "annotations": {"summary": "End-to-end relay smoke test"},
    "values": {"A": 1}
  }]
}
EOF
