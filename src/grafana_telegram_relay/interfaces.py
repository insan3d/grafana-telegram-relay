"""
Describe the asynchronous services used by incident delivery.

Structural protocols keep the Telegram transport replaceable in tests while
requiring the same argument and result types as the production API client.
"""

from collections.abc import Awaitable, Callable
from typing import Protocol
lazy from collections.abc import Sequence

lazy from telegram import InlineKeyboardMarkup, InputMediaPhoto, ReplyParameters, Update

from .models import Alert

type Render = Callable[[Alert], Awaitable[bytes | None]]
type ChatId = int | str


class MessageReference(Protocol):
    """Expose the identifier persisted after a successful Telegram send."""

    @property
    def message_id(self) -> int:
        """Return the chat-local Telegram message identifier."""

        ...


class TelegramClient(Protocol):
    """Define the Telegram operations for incident messages and recovery replies."""

    async def copy_message(
        self, *, chat_id: ChatId, from_chat_id: ChatId, message_id: int, reply_markup: InlineKeyboardMarkup | None
    ) -> MessageReference:
        """Create a reminder with the original content, media, and incident controls."""

        ...

    async def delete_message(self, *, chat_id: ChatId, message_id: int) -> bool:
        """Delete a tracked reminder from the configured chat."""

        ...

    async def send_message(
        self,
        *,
        chat_id: ChatId,
        text: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
        reply_parameters: ReplyParameters | None = None,
    ) -> MessageReference:
        """
        Send text and return the created message identifier.

        Args:
            chat_id: Target Telegram group or chat.
            text: Formatted incident details.
            parse_mode: Telegram formatting mode, or None for plain text.
            reply_markup: Shared incident action buttons.
            reply_parameters: Optional reference to the original incident message.

        Returns:
            Reference to the newly created message.
        """

        ...

    async def send_photo(
        self,
        *,
        chat_id: ChatId,
        photo: bytes,
        caption: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> MessageReference:
        """
        Send a screenshot with its formatted incident caption.

        Args:
            chat_id: Target Telegram group or chat.
            photo: PNG screenshot bytes.
            caption: Formatted incident details within the photo caption limit.
            parse_mode: Telegram formatting mode, or None for plain text.
            reply_markup: Shared incident action buttons.

        Returns:
            Reference to the newly created photo message.
        """

        ...

    async def edit_message_text(
        self,
        *,
        chat_id: ChatId,
        message_id: int,
        text: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> object:
        """
        Replace the text of an existing incident message.

        Args:
            chat_id: Target Telegram group or chat.
            message_id: Identifier persisted for this incident.
            text: Updated incident details.
            parse_mode: Telegram formatting mode, or None for plain text.
            reply_markup: Shared incident action buttons.

        Returns:
            Transport-specific API response, unused by delivery.
        """

        ...

    async def edit_message_caption(
        self,
        *,
        chat_id: ChatId,
        message_id: int,
        caption: str,
        parse_mode: str | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> object:
        """
        Replace the caption while retaining an existing screenshot.

        Args:
            chat_id: Target Telegram group or chat.
            message_id: Identifier persisted for this incident.
            caption: Updated incident details within the photo caption limit.
            parse_mode: Telegram formatting mode, or None for plain text.
            reply_markup: Shared incident action buttons.

        Returns:
            Transport-specific API response, unused by delivery.
        """

        ...

    async def edit_message_media(
        self, *, chat_id: ChatId, message_id: int, media: InputMediaPhoto, reply_markup: InlineKeyboardMarkup | None
    ) -> object:
        """
        Replace an incident screenshot and caption together.

        Args:
            chat_id: Target Telegram group or chat.
            message_id: Identifier persisted for this incident.
            media: Replacement screenshot with its caption and formatting mode.
            reply_markup: Shared incident action buttons.

        Returns:
            Transport-specific API response, unused by delivery.
        """

        ...


class CallbackClient(Protocol):
    """Expose long polling and private callback answers without a bot framework."""

    async def get_updates(
        self,
        *,
        offset: int,
        timeout: int,  # noqa: ASYNC109 -- Telegram long-poll duration, not a local deadline.
        allowed_updates: Sequence[str],
    ) -> tuple[Update, ...]:
        """Receive only requested update types after a durable offset."""

        ...

    async def answer_callback_query(self, callback_query_id: str, *, text: str) -> bool:
        """Acknowledge a button click with a private Telegram notification."""

        ...
