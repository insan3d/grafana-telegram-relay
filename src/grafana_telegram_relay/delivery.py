"""
Deliver persistent incident versions through a typed Telegram transport.

Each attempt operates on a version snapshot so an update received during
rendering remains pending. Screenshot and template failures retain text delivery.
"""

import logging
from datetime import timedelta
from time import monotonic

from telegram import InputMediaPhoto
from telegram.error import BadRequest, RetryAfter
lazy from telegram import InlineKeyboardMarkup

from .buttons import incident_keyboard, without_url_buttons
from .messages import Message, MessageTemplates
from .metrics import (
    DELIVERIES,
    DELIVERY_DURATION,
    RENDER_DURATION,
    RENDER_FAILURES,
    TEMPLATE_FAILURES,
)
from .models import Alert, Incident
lazy from .interfaces import ChatId, Render, TelegramClient
lazy from .store import Store

log = logging.getLogger(__name__)


class Delivery:
    """Coordinate formatting, screenshots, Telegram edits, and durable retries."""

    def __init__(
        self,
        store: Store,
        telegram: TelegramClient,
        chat_id: ChatId,
        render: Render | None,
        templates: MessageTemplates | None = None,
    ) -> None:
        """
        Initialize the incident delivery worker.

        Args:
            store: Persistent queue and retry state.
            telegram: Asynchronous Telegram API transport.
            chat_id: Target group or chat identifier.
            render: Optional screenshot provider.
            templates: Message formatter; the standard formatter is used by default.
        """

        self.store = store
        self.telegram = telegram
        self.chat_id = chat_id
        self.render = render
        self.templates = templates or MessageTemplates()

    async def once(self, now: float) -> bool:
        """
        Attempt one due update, scheduling a retry on failure.

        Args:
            now: Wall-clock time used to select due updates.

        Returns:
            Whether an update was selected, regardless of delivery success.
        """

        started = monotonic()
        row = await self.store.pending(now)

        if row is None:
            return False

        message_id, photo = row["message_id"], bool(row["photo"])
        result = "failure"

        try:
            item = Alert.model_validate_json(row["payload"])
            image = await self._image(item, row)
            message = self.templates(
                item, limit=1024 if image or photo else 4096, acknowledgement=row["acknowledgement"]
            )
            markup = incident_keyboard(item, row)

            message_id, photo = await self._send_safe(row, image, message, markup)

            await self.store.sent(row, message_id, photo=photo)
            result = "success"
            DELIVERIES.labels("success").inc()
            log.info(
                "Delivered incident %s, message %s, version %s",
                item.labels["alertname"],
                message_id,
                row["version"],
            )

        # Persist every failed attempt so a single incident cannot stop the worker.
        except Exception as error:  # noqa: BLE001
            delay: float = min(300, 2 ** min(row["attempts"] + 1, 9))

            if isinstance(error, RetryAfter):
                retry = error.retry_after
                delay = retry.total_seconds() if isinstance(retry, timedelta) else float(retry)

            await self.store.retry(row, now + monotonic() - started + delay, global_pause=isinstance(error, RetryAfter))
            DELIVERIES.labels("failure").inc()
            log.warning("Telegram delivery failed: %s; retry in %ss", type(error).__name__, delay)

        finally:
            DELIVERY_DURATION.labels(result).observe(monotonic() - started)

        return True

    async def _image(self, item: Alert, row: Incident) -> bytes | None:
        """Render alert updates while reusing the screenshot for acknowledgement-only edits."""

        ack = row["acknowledgement"]

        only_ack = ack is not None and ack["version"] == row["version"] and row["delivered"] == ack["version"] - 1

        if self.render is None or only_ack:
            return None

        started, result = monotonic(), "failure"

        try:
            image = await self.render(item)
            result = "success" if image else "skipped"

        # An optional renderer must never prevent incident delivery.
        except Exception as error:  # noqa: BLE001
            RENDER_FAILURES.inc()
            log.warning("Screenshot failed: %s", type(error).__name__)

            return None

        else:
            return image

        finally:
            RENDER_DURATION.labels(result).observe(monotonic() - started)

    async def _send_safe(
        self, row: Incident, image: bytes | None, message: Message, markup: InlineKeyboardMarkup | None
    ) -> tuple[int, bool]:
        """Retry rejected HTML or URL controls without blocking the incident itself."""

        while True:
            try:
                return await self.send(row, image, message, reply_markup=markup)

            except BadRequest as error:
                reason = str(error).lower()

                if message.parse_mode == "HTML" and "parse entities" in reason:
                    TEMPLATE_FAILURES.inc()
                    log.warning("Telegram rejected template HTML; sending plain text")
                    message = Message(message.plain, None, message.plain)

                elif markup is not None and ("button_url" in reason or "wrong http url" in reason):
                    remaining = without_url_buttons(markup)

                    if remaining == markup:
                        raise

                    log.warning("Telegram rejected navigation URLs; keeping incident controls")
                    markup = remaining

                else:
                    raise

    async def send(
        self, row: Incident, image: bytes | None, message: Message, *, reply_markup: InlineKeyboardMarkup | None
    ) -> tuple[int, bool]:
        """
        Send or edit an incident and recover from a deleted Telegram message.

        Args:
            row: Snapshot containing the current Telegram message identifier.
            image: Screenshot bytes, if rendering succeeded.
            message: Text or caption and its parse mode.
            reply_markup: Buttons matching this incident snapshot.

        Returns:
            Message identifier and whether that message contains a photo.
        """

        message_id, photo = row["message_id"], bool(row["photo"])

        if message_id:
            try:
                if image:
                    await self.telegram.edit_message_media(
                        chat_id=self.chat_id,
                        message_id=message_id,
                        media=InputMediaPhoto(media=image, caption=message.text, parse_mode=message.parse_mode),
                        reply_markup=reply_markup,
                    )
                    photo = True

                elif photo:
                    await self.telegram.edit_message_caption(
                        chat_id=self.chat_id,
                        message_id=message_id,
                        caption=message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=reply_markup,
                    )

                else:
                    await self.telegram.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=message_id,
                        text=message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=reply_markup,
                    )

            except BadRequest as error:
                reason = str(error).lower()

                if "message is not modified" in reason:
                    pass

                elif "message to edit not found" in reason:
                    message_id = None

                else:
                    raise

        if message_id is None:
            if image:
                sent = await self.telegram.send_photo(
                    chat_id=self.chat_id,
                    photo=image,
                    caption=message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=reply_markup,
                )
                photo = True

            else:
                sent = await self.telegram.send_message(
                    chat_id=self.chat_id,
                    text=message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=reply_markup,
                )
                photo = False

            message_id = sent.message_id

        return message_id, photo
