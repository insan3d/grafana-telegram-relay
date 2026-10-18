"""
Receive Telegram button callbacks and persist operator acknowledgements.

Polling confirms an update only after its state is durable. Replayed clicks are
idempotent, and transient Telegram failures do not stop webhook delivery.
"""

import asyncio
import logging
import time
from datetime import timedelta

from telegram import Message
from telegram.error import BadRequest, RetryAfter, TelegramError
lazy from telegram import CallbackQuery, Update

from .metrics import ACKNOWLEDGEMENTS, CALLBACK_FAILURES
lazy from .interfaces import CallbackClient, ChatId
lazy from .models import AckResult
lazy from .store import Store

log = logging.getLogger(__name__)
LONG_POLL_SECONDS = 30
POLL_RETRY_SECONDS = 5
ACK_MESSAGES: dict[AckResult, str] = {
    "acknowledged": "Acknowledged",
    "already_acknowledged": "Already acknowledged",
    "resolved": "Alert already resolved",
    "not_found": "Alert not found",
}


class Callbacks:
    """Poll button updates for the configured chat and queue durable message edits."""

    def __init__(self, store: Store, telegram: CallbackClient, chat_id: ChatId) -> None:
        """
        Initialize the callback consumer.

        Args:
            store: Incident acknowledgements and confirmed Telegram update offset.
            telegram: Long-polling and callback-answer transport.
            chat_id: Only this chat may acknowledge this bot's incident messages.
        """

        self.store = store
        self.telegram = telegram
        self.chat_id = chat_id

    def _matches_chat(self, message: Message) -> bool:
        """Match a numeric target chat or a configured public supergroup username."""

        if str(message.chat_id) == str(self.chat_id):
            return True

        username = message.chat.username

        return username is not None and ("@" + username).casefold() == str(self.chat_id).casefold()

    async def _acknowledge(self, query: CallbackQuery) -> AckResult:
        """Validate the message and token before accepting its operator identity."""

        message, data = query.message, query.data

        if not isinstance(message, Message) or not self._matches_chat(message):
            return "not_found"

        if not isinstance(data, str) or not data.startswith("ack:"):
            return "not_found"

        user = query.from_user
        name = "@" + user.username if user.username else user.full_name

        return await self.store.acknowledge(
            message.message_id,
            data.removeprefix("ack:"),
            user_id=user.id,
            name=name.strip() or str(user.id),
            now=time.time(),
        )

    async def handle(self, update: Update) -> None:
        """
        Apply an ACK and answer the clicker without posting another chat message.

        Args:
            update: Telegram update decoded by the API client.
        """

        query = update.callback_query

        if query is None:
            return

        result = await self._acknowledge(query)
        ACKNOWLEDGEMENTS.labels(result).inc()

        try:
            await self.telegram.answer_callback_query(query.id, text=ACK_MESSAGES[result])

        except BadRequest:
            # Expired popup answers must not block a durably accepted acknowledgement.
            CALLBACK_FAILURES.inc()
            log.debug("Telegram callback answer expired or was rejected")

    async def poll(self) -> None:
        """Consume one update batch and persist progress after each processed update."""

        offset = await self.store.telegram_offset()
        updates = await self.telegram.get_updates(
            offset=offset, timeout=LONG_POLL_SECONDS, allowed_updates=["callback_query"]
        )

        for update in updates:
            await self.handle(update)
            await self.store.advance_telegram_offset(update.update_id + 1)

    async def run(self) -> None:
        """Keep polling through transient Telegram failures until cancelled."""

        while True:
            try:
                await self.poll()

            except RetryAfter as error:
                CALLBACK_FAILURES.inc()
                retry = error.retry_after
                delay = retry.total_seconds() if isinstance(retry, timedelta) else float(retry)
                await asyncio.sleep(delay)

            except TelegramError as error:
                CALLBACK_FAILURES.inc()
                log.warning("Telegram callback polling failed: %s", type(error).__name__)
                await asyncio.sleep(POLL_RETRY_SECONDS)
