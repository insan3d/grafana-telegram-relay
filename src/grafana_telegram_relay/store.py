"""
Persist incident delivery, retry scheduling, and watchdog state in SQLite.

A single connection and lock serialize writes. Public methods return typed
snapshots so database-driver values cannot escape into application logic.
Tracked reminder IDs outlive pruned incident history until cleanup is confirmed.
"""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

import aiosqlite

from .grouping import notification
from .models import (
    ALERTS,
    TEXT,
    Acknowledgement,
    AckResult,
    Alert,
    Incident,
    ReminderTask,
    StoreMetrics,
    Webhook,
    acknowledgement_token,
    canary_alert,
)


def _value(row: sqlite3.Row, key: str | int) -> object:
    """Read a dynamically typed SQLite column at the driver boundary."""

    value: object = row[key]

    return value


def _integer(value: object) -> int:
    """Require an integer column instead of silently coercing corrupt state."""

    if not isinstance(value, int):
        msg = "expected an integer SQLite column"
        raise TypeError(msg)

    return value


def _number(value: object) -> float:
    """Require a numeric SQLite column and normalize its numeric type."""

    if not isinstance(value, (int, float)):
        msg = "expected a numeric SQLite column"
        raise TypeError(msg)

    return float(value)


async def _row(cursor: aiosqlite.Cursor) -> sqlite3.Row:
    """Fetch a required row from a query that must always return a result."""

    value: object = await cursor.fetchone()

    if value is None:
        msg = "required SQLite row is missing"
        raise RuntimeError(msg)

    if not isinstance(value, sqlite3.Row):
        msg = "unexpected SQLite row factory"
        raise TypeError(msg)

    return value


def _acknowledgement(row: sqlite3.Row) -> Acknowledgement | None:
    """Decode optional acknowledgement columns from an incident query."""

    name = _value(row, "ack_name")

    if name is None:
        return None

    return {
        "name": TEXT.validate_python(name),
        "user_id": _integer(_value(row, "ack_user_id")),
        "at": _number(_value(row, "ack_at")),
        "version": _integer(_value(row, "ack_version")),
    }


class Store:
    """Own the durable incident queue and independent canary state."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        """
        Initialize a store around an opened SQLite connection.

        Args:
            db: Connection configured to return SQLite rows.
        """

        self.db = db
        self.lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path, now: float) -> Self:
        """
        Open the database and initialize durable state.

        Args:
            path: SQLite database file location.
            now: Initial heartbeat timestamp for a new database.

        Returns:
            An initialized store owning the connection.
        """

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row

        await db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS incidents (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, delivered INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER, photo INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS incidents_message_id ON incidents(message_id);
            CREATE TABLE IF NOT EXISTS acknowledgements (
                incident_key TEXT PRIMARY KEY REFERENCES incidents(key) ON DELETE CASCADE,
                ack_user_id INTEGER NOT NULL, ack_name TEXT NOT NULL,
                ack_at REAL NOT NULL, ack_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resolution_replies (
                incident_key TEXT PRIMARY KEY REFERENCES incidents(key) ON DELETE CASCADE,
                message_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminder_schedule (
                incident_key TEXT PRIMARY KEY REFERENCES incidents(key) ON DELETE CASCADE,
                next_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reminder_messages (
                message_id INTEGER PRIMARY KEY, incident_key TEXT NOT NULL,
                sent_at REAL NOT NULL, next_at REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS reminder_messages_incident ON reminder_messages(incident_key);
            CREATE TABLE IF NOT EXISTS telegram_updates (
                id INTEGER PRIMARY KEY CHECK(id=1), offset INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO telegram_updates(id) VALUES(1);
            CREATE TABLE IF NOT EXISTS canary (
                id INTEGER PRIMARY KEY CHECK(id=1), last_seen REAL NOT NULL, outage_start REAL
            );
            CREATE TABLE IF NOT EXISTS delivery (
                id INTEGER PRIMARY KEY CHECK(id=1), pause_until REAL NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO delivery(id) VALUES(1);
        """)

        for table, column in (
            ("incidents", "generation"),
            ("incidents", "message_generation"),
            ("reminder_messages", "generation"),
        ):
            async with db.execute(f"PRAGMA table_info({table})") as cursor:
                columns = {TEXT.validate_python(_value(row, "name")) for row in await cursor.fetchall()}

            if column not in columns:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS group_heads (
                group_id TEXT PRIMARY KEY, incident_key TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL, member_key TEXT NOT NULL,
                incident_key TEXT NOT NULL REFERENCES incidents(key) ON DELETE CASCADE,
                payload TEXT NOT NULL, PRIMARY KEY(group_id,member_key)
            );
            CREATE INDEX IF NOT EXISTS group_members_incident ON group_members(incident_key);
        """)
        await db.execute("INSERT OR IGNORE INTO canary(id,last_seen) VALUES(1,?)", (now,))
        await db.commit()

        return cls(db)

    async def close(self) -> None:
        """Close the owned database connection."""

        await self.db.close()

    async def ready(self) -> None:
        """
        Verify that SQLite accepts and commits a write transaction.

        Raises:
            aiosqlite.Error: The connection cannot write or commit.
        """

        async with self.lock:
            try:
                await self.db.execute("UPDATE delivery SET pause_until=pause_until WHERE id=1")
                await self.db.commit()

            except BaseException:
                await self.db.rollback()
                raise

    async def metrics(self) -> StoreMetrics:
        """
        Read queue and heartbeat gauges as one consistent snapshot.

        Returns:
            Durable counts, queue age origin, and last heartbeat time.
        """

        async with self.lock:
            async with self.db.execute(
                "SELECT coalesce(sum(delivered<version),0) AS pending, "
                "coalesce(sum(json_extract(payload,'$.status')='firing'),0) AS active, "
                "coalesce(min(CASE WHEN delivered<version THEN updated END),0) AS oldest_pending "
                "FROM incidents"
            ) as cursor:
                row = await _row(cursor)
                state: StoreMetrics = {
                    "pending": _integer(_value(row, "pending")),
                    "active": _integer(_value(row, "active")),
                    "oldest_pending": _number(_value(row, "oldest_pending")),
                    "last_seen": 0.0,
                }

            async with self.db.execute("SELECT last_seen FROM canary WHERE id=1") as cursor:
                state["last_seen"] = _number(_value(await _row(cursor), 0))

            return state

    async def _upsert(self, key: str, item: Alert, now: float) -> None:
        """Queue a changed alert while keeping resolved incidents terminal."""

        payload = json.dumps(item.model_dump(by_alias=True), sort_keys=True, ensure_ascii=False)
        await self.db.execute(
            """
            INSERT INTO incidents(key,payload,updated) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                payload=excluded.payload, version=incidents.version+1, updated=excluded.updated,
                next_attempt=0, attempts=0
            WHERE incidents.payload != excluded.payload
              AND json_extract(incidents.payload,'$.status') != 'resolved'
        """,
            (key, payload, now),
        )

    async def ingest(self, items: object, now: float) -> None:
        """
        Validate and persist a webhook batch before acknowledging it.

        Args:
            items: Nonempty array of untrusted Grafana alerts.
            now: Timestamp of the received update.

        Raises:
            TypeError: A batch entry has an invalid type; no entries are written.
            ValueError: A batch entry has invalid values; no entries are written.
        """

        webhook = items if isinstance(items, Webhook) else Webhook(alerts=ALERTS.validate_python(items))

        async with self.lock:
            try:
                if webhook.group_key:
                    await self._ingest_group(webhook, now)

                else:
                    for item in webhook.alerts:
                        await self._upsert(item.key, item, now)

                await self.db.commit()

            except BaseException:
                await self.db.rollback()
                raise

    async def _ingest_group(self, webhook: Webhook, now: float) -> None:
        """Atomically route explicit member episodes and rebuild affected group snapshots."""

        identity = webhook.group_identity

        async with self.db.execute(
            "SELECT incident_key FROM group_heads JOIN incidents ON key=incident_key "
            "WHERE group_id=? AND json_extract(payload,'$.status')='firing'",
            (identity,),
        ) as cursor:
            head = await cursor.fetchone()

        current = TEXT.validate_python(_value(head, 0)) if head is not None else None
        changed: dict[str, bool] = {}

        for item in sorted(webhook.alerts, key=lambda item: item.status != "resolved"):
            async with self.db.execute("SELECT key FROM incidents WHERE key=?", (item.key,)) as cursor:
                legacy = await cursor.fetchone()

            if legacy is not None:
                await self._upsert(item.key, item, now)
                continue

            async with self.db.execute(
                "SELECT incident_key,payload FROM group_members WHERE group_id=? AND member_key=?",
                (identity, item.key),
            ) as cursor:
                previous = await cursor.fetchone()

            if previous is not None:
                old = Alert.model_validate_json(TEXT.validate_python(_value(previous, "payload")))

                if old.status == "resolved" or old == item:
                    continue

                key = TEXT.validate_python(_value(previous, "incident_key"))
                new_failure = False

            else:
                if current is None:
                    current = "group:" + identity + ":" + uuid4().hex
                    await self._upsert(current, notification([item], webhook), now)
                    await self.db.execute(
                        "INSERT INTO group_heads VALUES(?,?) ON CONFLICT(group_id) "
                        "DO UPDATE SET incident_key=excluded.incident_key",
                        (identity, current),
                    )

                key = current
                new_failure = item.status == "firing"

            await self.db.execute(
                "INSERT INTO group_members VALUES(?,?,?,?) ON CONFLICT(group_id,member_key) "
                "DO UPDATE SET payload=excluded.payload",
                (identity, item.key, key, item.model_dump_json(by_alias=True)),
            )
            changed[key] = changed.get(key, False) or new_failure

        for key, new_failure in changed.items():
            async with self.db.execute("SELECT payload FROM group_members WHERE incident_key=?", (key,)) as cursor:
                members = [
                    Alert.model_validate_json(TEXT.validate_python(_value(row, 0))) for row in await cursor.fetchall()
                ]

            aggregate = notification(members, webhook)

            # New group snapshots can start resolved while a mixed batch is assembled.
            await self.db.execute(
                "UPDATE incidents SET payload=?,version=version+1,updated=?,next_attempt=0,attempts=0 WHERE key=?",
                (aggregate.model_dump_json(by_alias=True), now, key),
            )

            if new_failure:
                await self.db.execute("DELETE FROM acknowledgements WHERE incident_key=?", (key,))
                await self.db.execute("UPDATE incidents SET generation=generation+1 WHERE key=?", (key,))
                await self.db.execute(
                    "UPDATE reminder_schedule SET next_at=?,attempts=0 WHERE incident_key=?", (now, key)
                )

    async def pending(self, now: float) -> Incident | None:
        """
        Select the oldest due update while respecting a global API pause.

        Args:
            now: Timestamp used to evaluate retry deadlines.

        Returns:
            A delivery snapshot, or None when nothing is due.
        """

        async with (
            self.lock,
            self.db.execute(
                "SELECT incidents.*,ack_user_id,ack_name,ack_at,ack_version, "
                "resolution_replies.message_id AS resolution_message_id FROM incidents "
                "LEFT JOIN acknowledgements ON acknowledgements.incident_key=incidents.key "
                "LEFT JOIN resolution_replies ON resolution_replies.incident_key=incidents.key "
                "WHERE delivered < version AND next_attempt <= ? "
                "AND (SELECT pause_until FROM delivery WHERE id=1) <= ? "
                "ORDER BY next_attempt,updated LIMIT 1",
                (now, now),
            ) as cursor,
        ):
            row = await cursor.fetchone()

        if row is None:
            return None

        msg = "unexpected SQLite row factory"

        if not isinstance(row, sqlite3.Row):
            raise TypeError(msg)

        message_id = _value(row, "message_id")
        resolution_message_id = _value(row, "resolution_message_id")

        return {
            "key": TEXT.validate_python(_value(row, "key")),
            "payload": TEXT.validate_python(_value(row, "payload")),
            "version": _integer(_value(row, "version")),
            "delivered": _integer(_value(row, "delivered")),
            "message_id": _integer(message_id) if message_id is not None else None,
            "resolution_message_id": _integer(resolution_message_id) if resolution_message_id is not None else None,
            "photo": _integer(_value(row, "photo")),
            "attempts": _integer(_value(row, "attempts")),
            "next_attempt": _number(_value(row, "next_attempt")),
            "updated": _number(_value(row, "updated")),
            "acknowledgement": _acknowledgement(row),
            "generation": _integer(_value(row, "generation")),
        }

    async def acknowledge(self, message_id: int, token: str, *, user_id: int, name: str, now: float) -> AckResult:
        """
        Persist the first ACK and queue an edit of the same incident message.

        Args:
            message_id: Message selected by a callback in the configured chat.
            token: Opaque incident token read from the clicked button.
            user_id: Telegram identifier of the operator.
            name: Display name or username, including its optional at-sign.
            now: Timestamp when acknowledgement was accepted.

        Returns:
            Outcome used for the private callback notification.
        """

        async with self.lock:
            async with self.db.execute(
                "SELECT key,version,generation,message_generation,json_extract(payload,'$.status') AS status,ack_name "
                "FROM incidents "
                "LEFT JOIN acknowledgements ON acknowledgements.incident_key=incidents.key "
                "WHERE message_id=? OR key=(SELECT incident_key FROM reminder_messages WHERE message_id=?)",
                (message_id, message_id),
            ) as cursor:
                row = await cursor.fetchone()

            if (
                row is None
                or acknowledgement_token(TEXT.validate_python(_value(row, "key")), _integer(_value(row, "generation")))
                != token
                or _value(row, "generation") != _value(row, "message_generation")
            ):
                return "not_found"

            if _value(row, "status") != "firing":
                return "resolved"

            if _value(row, "ack_name") is not None:
                return "already_acknowledged"

            key = TEXT.validate_python(_value(row, "key"))
            version = _integer(_value(row, "version")) + 1
            await self.db.execute(
                "INSERT INTO acknowledgements(incident_key,ack_user_id,ack_name,ack_at,ack_version) VALUES(?,?,?,?,?)",
                (key, user_id, name, now, version),
            )
            await self.db.execute(
                "UPDATE incidents SET version=?,updated=?,next_attempt=0,attempts=0 WHERE key=?",
                (version, now, key),
            )
            await self.db.commit()

            return "acknowledged"

    async def telegram_offset(self) -> int:
        """Read the first unconfirmed Telegram update identifier."""

        async with (
            self.lock,
            self.db.execute("SELECT offset FROM telegram_updates WHERE id=1") as cursor,
        ):
            return _integer(_value(await _row(cursor), 0))

    async def advance_telegram_offset(self, offset: int) -> None:
        """Persist a confirmed Telegram update boundary without moving backwards."""

        async with self.lock:
            await self.db.execute("UPDATE telegram_updates SET offset=max(offset,?) WHERE id=1", (offset,))
            await self.db.commit()

    async def save_message(self, row: Incident, message_id: int, *, photo: bool) -> None:
        """
        Preserve a replacement incident message while its recovery reply is pending.

        Args:
            row: Snapshot whose incident message was replaced.
            message_id: Telegram identifier of the replacement message.
            photo: Whether the replacement contains a screenshot.
        """

        async with self.lock:
            if row["photo"] and not photo and row["message_id"] is not None:
                await self.db.execute(
                    "INSERT OR IGNORE INTO reminder_messages(message_id,incident_key,sent_at,generation) "
                    "VALUES(?,?,?,-1)",
                    (row["message_id"], row["key"], row["updated"]),
                )

            await self.db.execute(
                "UPDATE incidents SET message_id=?,photo=? WHERE key=?",
                (message_id, int(photo), row["key"]),
            )
            await self.db.commit()

    async def resolution_sent(self, row: Incident, message_id: int) -> None:
        """
        Persist a confirmed recovery reply independently of incident completion.

        Args:
            row: Resolved incident snapshot.
            message_id: Telegram identifier returned for the recovery reply.
        """

        async with self.lock:
            await self.db.execute(
                "INSERT INTO resolution_replies(incident_key,message_id) VALUES(?,?) "
                "ON CONFLICT(incident_key) DO NOTHING",
                (row["key"], message_id),
            )
            await self.db.commit()

    async def sent(self, row: Incident, message_id: int, *, photo: bool, reminder_at: float | None = None) -> None:
        """
        Acknowledge exactly the version sent by a delivery attempt.

        Args:
            row: Snapshot that was delivered.
            message_id: Telegram message identifier.
            photo: Whether the message currently contains a screenshot.
            reminder_at: First reminder deadline when repeating is enabled.
        """

        async with self.lock:
            await self.db.execute(
                "UPDATE incidents SET delivered=?,message_id=?,photo=?,message_generation=?,"
                "attempts=0,next_attempt=0 WHERE key=?",
                (row["version"], message_id, int(photo), row["generation"], row["key"]),
            )

            if reminder_at is not None:
                await self.db.execute(
                    "INSERT INTO reminder_schedule(incident_key,next_at) VALUES(?,?) "
                    "ON CONFLICT(incident_key) DO NOTHING",
                    (row["key"], reminder_at),
                )

            await self.db.commit()

    async def reminder_task(self, now: float, interval: float = 0) -> ReminderTask | None:
        """
        Select cleanup before a due reminder while respecting Telegram's API pause.

        Args:
            now: Timestamp used for deadlines and day-old copy rotation.
            interval: Repeat interval; zero permits only deletion of existing copies.

        Returns:
            One due copy or deletion, or None when there is no eligible work.
        """

        async with self.lock:
            async with self.db.execute("SELECT pause_until FROM delivery WHERE id=1") as cursor:
                if _number(_value(await _row(cursor), 0)) > now:
                    return None

            async with self.db.execute(
                "SELECT reminder_messages.*,coalesce(incidents.payload,'{}') AS payload FROM reminder_messages "
                "LEFT JOIN incidents ON incidents.key=reminder_messages.incident_key "
                "LEFT JOIN acknowledgements ON acknowledgements.incident_key=reminder_messages.incident_key "
                "WHERE reminder_messages.next_at<=? AND (incidents.key IS NULL OR ack_name IS NOT NULL "
                "OR json_extract(payload,'$.status')='resolved' "
                "OR reminder_messages.generation!=incidents.generation OR sent_at<=?) "
                "ORDER BY reminder_messages.next_at,sent_at LIMIT 1",
                (now, now - 86400),
            ) as cursor:
                row: object = await cursor.fetchone()

            action: Literal["send", "delete"] = "delete"

            if row is None and interval > 0:
                await self.db.execute(
                    "INSERT OR IGNORE INTO reminder_schedule(incident_key,next_at) "
                    "SELECT key,? FROM incidents LEFT JOIN acknowledgements ON acknowledgements.incident_key=key "
                    "WHERE message_id IS NOT NULL AND delivered=version AND ack_name IS NULL "
                    "AND json_extract(payload,'$.status')='firing'",
                    (now + interval,),
                )
                await self.db.commit()

                async with self.db.execute(
                    "SELECT reminder_schedule.incident_key,incidents.payload,incidents.message_id,"
                    "incidents.generation, "
                    "reminder_schedule.attempts FROM reminder_schedule "
                    "JOIN incidents ON incidents.key=reminder_schedule.incident_key "
                    "LEFT JOIN acknowledgements ON acknowledgements.incident_key=incidents.key "
                    "WHERE reminder_schedule.next_at<=? AND incidents.message_id IS NOT NULL "
                    "AND delivered=version AND ack_name IS NULL AND json_extract(payload,'$.status')='firing' "
                    "ORDER BY reminder_schedule.next_at LIMIT 1",
                    (now,),
                ) as cursor:
                    row = await cursor.fetchone()

                action = "send"

            if row is None:
                return None

            if not isinstance(row, sqlite3.Row):
                msg = "unexpected SQLite row factory"
                raise TypeError(msg)

            return {
                "action": action,
                "generation": _integer(_value(row, "generation")),
                "key": TEXT.validate_python(_value(row, "incident_key")),
                "payload": TEXT.validate_python(_value(row, "payload")),
                "message_id": _integer(_value(row, "message_id")),
                "attempts": _integer(_value(row, "attempts")),
            }

    async def reminder_sent(self, task: ReminderTask, message_id: int, *, now: float, next_at: float) -> None:
        """
        Track a confirmed copy even if its incident was acknowledged while sending.

        Args:
            task: Reminder snapshot selected before the Telegram call.
            message_id: Identifier of the new duplicate.
            now: Timestamp when Telegram confirmed the copy.
            next_at: Deadline of the next repeat.
        """

        async with self.lock:
            await self.db.execute(
                "INSERT INTO reminder_messages(message_id,incident_key,sent_at,generation) VALUES(?,?,?,?)",
                (message_id, task["key"], now, task["generation"]),
            )
            await self.db.execute(
                "UPDATE reminder_schedule SET next_at=?,attempts=0 WHERE incident_key=?",
                (next_at, task["key"]),
            )
            await self.db.commit()

    async def reminder_deleted(self, message_id: int) -> None:
        """Forget a duplicate only after deletion succeeded or Telegram reports it missing."""

        async with self.lock:
            await self.db.execute("DELETE FROM reminder_messages WHERE message_id=?", (message_id,))
            await self.db.commit()

    async def redeliver(self, key: str, now: float) -> None:
        """
        Queue replacement of a missing original while its incident remains unacknowledged.

        Args:
            key: Incident whose source message Telegram could not copy.
            now: Timestamp when the missing message was detected.
        """

        async with self.lock:
            await self.db.execute(
                "UPDATE incidents SET version=version+1,updated=?,next_attempt=0,attempts=0 "
                "WHERE key=? AND delivered=version AND json_extract(payload,'$.status')='firing' "
                "AND NOT EXISTS(SELECT 1 FROM acknowledgements WHERE incident_key=incidents.key)",
                (now, key),
            )
            await self.db.commit()

    async def reminder_retry(self, task: ReminderTask, when: float, *, global_pause: bool = False) -> None:
        """
        Retain failed reminder work and optionally pause all outgoing API operations.

        Args:
            task: Copy or deletion that failed.
            when: Earliest retry timestamp.
            global_pause: Whether Telegram rate limiting requires a shared pause.
        """

        async with self.lock:
            if task["action"] == "send":
                await self.db.execute(
                    "UPDATE reminder_schedule SET next_at=?,attempts=attempts+1 WHERE incident_key=?",
                    (when, task["key"]),
                )

            else:
                await self.db.execute(
                    "UPDATE reminder_messages SET next_at=?,attempts=attempts+1 WHERE message_id=?",
                    (when, task["message_id"]),
                )

            if global_pause:
                await self.db.execute("UPDATE delivery SET pause_until=? WHERE id=1", (when,))

            await self.db.commit()

    async def retry(self, row: Incident, when: float, *, global_pause: bool = False) -> None:
        """
        Persist the next retry deadline.

        Args:
            row: Failed delivery snapshot.
            when: Earliest timestamp for another attempt.
            global_pause: Whether Telegram rate limiting pauses all deliveries.
        """

        async with self.lock:
            await self.db.execute(
                "UPDATE incidents SET attempts=attempts+1,next_attempt=? WHERE key=?",
                (when, row["key"]),
            )

            if global_pause:
                await self.db.execute("UPDATE delivery SET pause_until=? WHERE id=1", (when,))

            await self.db.commit()

    async def pending_count(self) -> int:
        """Return the number of incidents with undelivered updates."""

        async with (
            self.lock,
            self.db.execute("SELECT count(*) FROM incidents WHERE delivered < version") as cursor,
        ):
            return _integer(_value(await _row(cursor), 0))

    async def disable_canary(self, now: float) -> None:
        """
        Discard unfinished watchdog incidents and reset the heartbeat deadline.

        Args:
            now: Startup timestamp while the optional watchdog is disabled.
        """

        async with self.lock:
            await self.db.execute(
                "DELETE FROM incidents WHERE json_extract(payload,'$.fingerprint')='internal-grafana-canary' "
                "AND (delivered<version OR json_extract(payload,'$.status')='firing')"
            )
            await self.db.execute("UPDATE canary SET last_seen=?,outage_start=NULL WHERE id=1", (now,))
            await self.db.commit()

    async def heartbeat(self, now: float) -> None:
        """
        Persist a heartbeat and atomically resolve a watchdog outage.

        Args:
            now: Receipt time of a validated firing canary notification.
        """

        async with self.lock:
            async with self.db.execute("SELECT outage_start FROM canary WHERE id=1") as cursor:
                value = _value(await _row(cursor), 0)
                start = _number(value) if value is not None else None

            if start is not None:
                item = canary_alert(start, "resolved", now)
                await self._upsert(item.key, item, now)

            await self.db.execute("UPDATE canary SET last_seen=?,outage_start=NULL WHERE id=1", (now,))
            await self.db.commit()

    async def check_canary(self, now: float, max_silence: float) -> float:
        """
        Create one outage incident after the heartbeat timeout expires.

        Args:
            now: Current timestamp.
            max_silence: Maximum allowed heartbeat silence in seconds.

        Returns:
            Timestamp of the last persisted heartbeat.
        """

        async with self.lock:
            async with self.db.execute("SELECT * FROM canary WHERE id=1") as cursor:
                state = await _row(cursor)
                last_seen = _number(_value(state, "last_seen"))
                outage_start = _value(state, "outage_start")

            if now - last_seen > max_silence and outage_start is None:
                item = canary_alert(now)
                await self._upsert(item.key, item, now)
                await self.db.execute("UPDATE canary SET outage_start=? WHERE id=1", (now,))
                await self.db.commit()

            return last_seen

    async def cleanup(self, before: float) -> None:
        """
        Delete delivered resolved incidents older than the cutoff.

        Args:
            before: Oldest retained update timestamp.
        """

        async with self.lock:
            await self.db.execute(
                "DELETE FROM incidents WHERE updated < ? AND delivered=version "
                "AND json_extract(payload,'$.status')='resolved'",
                (before,),
            )
            await self.db.execute("DELETE FROM group_heads WHERE incident_key NOT IN (SELECT key FROM incidents)")
            await self.db.commit()
