"""Provide offline Telegram transports with recorded messages and controlled updates."""

import asyncio
import copy
from dataclasses import dataclass
from typing import TypedDict
lazy from collections.abc import Sequence

from telegram import InlineKeyboardMarkup, InputFile, InputMediaPhoto
from telegram.error import BadRequest, NetworkError, RetryAfter
lazy from telegram import Bot, ReplyParameters, Update

lazy from grafana_telegram_relay.interfaces import ChatId


async def idle_updates(_bot: Bot, **_kwargs: object) -> tuple[Update, ...]:
    """Keep production polling alive without retrieving updates from Telegram."""

    await asyncio.Event().wait()

    return ()


class MessageRecord(TypedDict, total=False):
    """Capture Telegram arguments for assertions without an HTTP transport."""

    chat_id: ChatId
    text: str
    caption: str
    photo: bytes | InputFile | str
    parse_mode: str | None
    reply_markup: InlineKeyboardMarkup | None
    reply_parameters: ReplyParameters | None


@dataclass(frozen=True)
class SentMessage:
    """Expose the message identifier returned by the fake transport."""

    message_id: int


class Telegram:
    """Keep sent messages in memory and implement the delivery transport contract."""

    def __init__(self) -> None:
        """Initialize an empty chat with sequential message identifiers."""

        self.messages: dict[int, MessageRecord] = {}
        self.next_id = 1
        self.fail = False
        self.fail_replies = False

    async def send_message(
        self,
        *,
        chat_id: ChatId,
        text: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None = None,
        reply_parameters: ReplyParameters | None = None,
    ) -> SentMessage:
        """Record text unless the simulated Telegram outage is enabled."""

        if self.fail or (reply_parameters is not None and self.fail_replies):
            msg = "unavailable"
            raise NetworkError(msg)

        return self.send(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "reply_parameters": reply_parameters,
            }
        )

    async def send_photo(
        self,
        *,
        chat_id: ChatId,
        photo: bytes,
        caption: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> SentMessage:
        """Record a screenshot and its caption as one message."""

        return self.send(
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )

    def send(self, record: MessageRecord) -> SentMessage:
        """Persist a copy of sent arguments and allocate its identifier."""

        message_id = self.next_id
        self.next_id += 1
        self.messages[message_id] = copy.deepcopy(record)

        return SentMessage(message_id)

    def message(self, message_id: int) -> MessageRecord:
        """Find an editable message or reproduce Telegram's deleted-message response."""

        try:
            return self.messages[message_id]

        except KeyError as error:
            msg = "Message to edit not found"
            raise BadRequest(msg) from error

    async def edit_message_text(
        self,
        *,
        chat_id: ChatId,
        message_id: int,
        text: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Replace text in the selected recorded message."""

        self.message(message_id).update(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "reply_markup": reply_markup}
        )

    async def edit_message_media(
        self,
        *,
        chat_id: ChatId,
        message_id: int,
        media: InputMediaPhoto,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Replace recorded media and caption using Telegram's media object."""

        photo = media.media
        msg = "expected uploaded photo or file identifier"

        if not isinstance(photo, (bytes, InputFile, str)):
            raise TypeError(msg)

        self.message(message_id).update(
            {"chat_id": chat_id, "caption": media.caption or "", "photo": photo, "reply_markup": reply_markup}
        )

    async def edit_message_caption(
        self,
        *,
        chat_id: ChatId,
        message_id: int,
        caption: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Replace the caption without discarding the recorded photo."""

        self.message(message_id).update(
            {"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode, "reply_markup": reply_markup}
        )


class TelegramUpdates:
    """Return controlled update batches and record private callback answers."""

    def __init__(self) -> None:
        """Initialize empty batches and deterministic request controls."""

        self.batches: list[tuple[Update, ...]] = []
        self.requests: list[tuple[int, int, tuple[str, ...]]] = []
        self.answers: list[tuple[str, str]] = []
        self.waiting = asyncio.Event()
        self.rate_limit = False
        self.expired = False

    async def get_updates(
        self,
        *,
        offset: int,
        timeout: int,  # noqa: ASYNC109 -- Telegram long-poll duration, not a local deadline.
        allowed_updates: Sequence[str],
    ) -> tuple[Update, ...]:
        """Return one queued batch, optionally rate limit, then wait for cancellation."""

        self.requests.append((offset, timeout, tuple(allowed_updates)))

        if self.rate_limit:
            self.rate_limit = False
            raise RetryAfter(0)

        if self.batches:
            return self.batches.pop(0)

        self.waiting.set()
        await asyncio.Event().wait()

        return ()

    async def answer_callback_query(self, callback_query_id: str, *, text: str) -> bool:
        """Record a private response or simulate an expired Telegram callback."""

        if self.expired:
            msg = "Query is too old and response timeout expired"
            raise BadRequest(msg)

        self.answers.append((callback_query_id, text))

        return True
