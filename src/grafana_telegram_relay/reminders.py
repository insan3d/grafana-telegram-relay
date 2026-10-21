"""
Repeat unacknowledged incident messages and remove their tracked duplicates.

Copy identifiers and retry deadlines are durable. Cleanup also runs while repeats
are disabled, and copies older than one day are rotated before Telegram's limit.
"""

import logging
from datetime import timedelta
from time import monotonic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from .metrics import REMINDERS
from .models import Alert, acknowledgement_token
lazy from .interfaces import ChatId, TelegramClient
lazy from .models import ReminderTask
lazy from .store import Store

log = logging.getLogger(__name__)


class Reminders:
    """Perform one due copy or cleanup operation without a second delivery worker."""

    def __init__(self, store: Store, telegram: TelegramClient, chat_id: ChatId, interval: float = 0) -> None:
        """Bind persistent state and transport to the configured repeat interval."""

        self.store = store
        self.telegram = telegram
        self.chat_id = chat_id
        self.interval = interval

    async def once(self, now: float, *, send: bool = True) -> bool:
        """Process a due action with durable retries and a shared Telegram rate-limit pause."""

        task = await self.store.reminder_task(now, self.interval if send else 0)

        if task is None:
            return False

        started = monotonic()

        try:
            await self._perform(task, now)

        # Optional reminders must not stop delivery of original incident updates.
        except Exception as error:  # noqa: BLE001
            delay: float = min(300, 2 ** min(task["attempts"] + 1, 9))

            if (
                task["action"] == "send"
                and isinstance(error, BadRequest)
                and "message to copy not found" in str(error).lower()
            ):
                await self.store.redeliver(task["key"], now)

            if isinstance(error, RetryAfter):
                retry = error.retry_after
                delay = retry.total_seconds() if isinstance(retry, timedelta) else float(retry)

            await self.store.reminder_retry(
                task, now + monotonic() - started + delay, global_pause=isinstance(error, RetryAfter)
            )
            REMINDERS.labels(task["action"], "failure").inc()
            log.warning("Reminder %s failed: %s; retry in %ss", task["action"], type(error).__name__, delay)

        else:
            REMINDERS.labels(task["action"], "success").inc()

        return True

    async def _perform(self, task: ReminderTask, now: float) -> None:
        """Delete a tracked copy or copy the latest original with a shared ACK token."""

        if task["action"] == "delete":
            try:
                await self.telegram.delete_message(chat_id=self.chat_id, message_id=task["message_id"])

            except BadRequest as error:
                if "message to delete not found" not in str(error).lower():
                    raise

            await self.store.reminder_deleted(task["message_id"])

        else:
            item = Alert.model_validate_json(task["payload"])
            data = "ack:" + acknowledgement_token(item.key)
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Acknowledge", callback_data=data)]])
            started = monotonic()
            message = await self.telegram.copy_message(
                chat_id=self.chat_id,
                from_chat_id=self.chat_id,
                message_id=task["message_id"],
                reply_markup=markup,
            )
            completed = now + monotonic() - started
            await self.store.reminder_sent(task, message.message_id, now=completed, next_at=completed + self.interval)
