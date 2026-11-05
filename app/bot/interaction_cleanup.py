from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

MESSAGE_FLOW_IDS_KEY = "message_flow_message_ids"
logger = logging.getLogger(__name__)


def _message_ids(messages: tuple[Message | None, ...]) -> list[int]:
    return [
        message_id
        for message in messages
        if message is not None
        if (message_id := getattr(message, "message_id", None)) is not None
    ]


async def reset_message_flow(
    state: FSMContext,
    *messages: Message | None,
    preserve_if_tracked: bool = False,
) -> None:
    data = await state.get_data()
    existing = [int(value) for value in data.get(MESSAGE_FLOW_IDS_KEY, [])]
    current = _message_ids(messages)
    if not preserve_if_tracked or not set(existing).intersection(current):
        existing = []
    await state.clear()
    await state.update_data(
        **{MESSAGE_FLOW_IDS_KEY: list(dict.fromkeys([*existing, *current]))}
    )


async def track_message_flow(state: FSMContext, *messages: Message | None) -> None:
    data = await state.get_data()
    existing = [int(value) for value in data.get(MESSAGE_FLOW_IDS_KEY, [])]
    await state.update_data(
        **{
            MESSAGE_FLOW_IDS_KEY: list(
                dict.fromkeys([*existing, *_message_ids(messages)])
            )
        }
    )


async def tracked_answer(
    message: Message,
    state: FSMContext,
    text: str,
    **kwargs: Any,
) -> Message:
    await track_message_flow(state, message)
    sent = await message.answer(text, **kwargs)
    await track_message_flow(state, sent)
    return sent


async def finish_message_flow(
    state: FSMContext,
    bot: Any,
    chat_id: int,
    *messages: Message | None,
) -> bool:
    data = await state.get_data()
    recorded = [int(value) for value in data.get(MESSAGE_FLOW_IDS_KEY, [])]
    await state.clear()
    if not recorded:
        return False
    message_ids = list(dict.fromkeys([*recorded, *_message_ids(messages)]))
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=message_ids)
    except TelegramAPIError as exc:
        logger.warning("Failed to delete completed message-flow interactions: %s", exc)
    return True
