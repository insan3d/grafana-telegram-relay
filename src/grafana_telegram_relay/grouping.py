"""Build deterministic notification snapshots from explicitly tracked member episodes."""

from .models import Notification
lazy from .models import Alert, Webhook


def common_fields(values: list[dict[str, str]]) -> dict[str, str]:
    """Keep only metadata whose exact value agrees across every member."""

    return {key: value for key, value in values[0].items() if all(item.get(key) == value for item in values)}


def notification(members: list[Alert], webhook: Webhook) -> Notification:
    """Aggregate member state without selecting arbitrary navigation or template metadata."""

    members = sorted(members, key=lambda item: (item.status != "firing", item.key))
    data = members[0].model_dump(by_alias=True)

    if len(members) > 1:
        labels = common_fields([item.labels for item in members])
        labels.setdefault("alertname", "Grafana group")
        mentions = sorted({item.labels["mention"] for item in members if item.labels.get("mention")})

        if mentions:
            labels["mention"] = " ".join(mentions)

        data.update(
            labels=labels,
            annotations=common_fields([item.annotations for item in members]),
            values={},
            fingerprint=webhook.group_identity,
            panelURL="",
            dashboardURL="",
            generatorURL="",
            silenceURL="",
        )

    firing = any(item.status == "firing" for item in members)
    data.update(
        status="firing" if firing else "resolved",
        startsAt=min(item.starts_at for item in members),
        endsAt="" if firing else max(item.ends_at for item in members),
        alerts=[item.model_dump(by_alias=True) for item in members],
        groupKey=webhook.group_key,
        receiver=webhook.receiver,
        externalURL=webhook.external_url,
        orgId=webhook.org_id,
        groupLabels=webhook.group_labels,
    )

    return Notification.model_validate(data)
