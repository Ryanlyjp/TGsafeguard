from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import getaddresses
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import TempMailMessage

ALLOWED_TEMP_MAIL_DOMAINS = frozenset(settings.login_email_alias_domains)
_message_events: dict[str, asyncio.Event] = {}


def temp_mail_message_event(recipient: str) -> asyncio.Event:
    normalized = recipient.strip().lower()
    event = _message_events.get(normalized)
    if event is None:
        event = asyncio.Event()
        _message_events[normalized] = event
    return event


def notify_temp_mail_recipient(recipient: str) -> None:
    event = _message_events.get(recipient.strip().lower())
    if event is not None:
        event.set()


def release_temp_mail_message_event(recipient: str, event: asyncio.Event) -> None:
    normalized = recipient.strip().lower()
    if _message_events.get(normalized) is event:
        _message_events.pop(normalized, None)


def normalize_recipient(
    address: str,
    allowed_domains: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[str, str]:
    normalized = address.strip().lower()
    if len(normalized) > 320 or normalized.count("@") != 1:
        raise ValueError("invalid recipient address")
    local_part, domain = normalized.rsplit("@", 1)
    allowed = ALLOWED_TEMP_MAIL_DOMAINS if allowed_domains is None else frozenset(allowed_domains)
    if not local_part or len(local_part) > 64 or domain not in allowed:
        raise ValueError("recipient domain is not allowed")
    return normalized, domain


def parse_allowed_recipients(
    value: object,
    allowed_domains: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        headers = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        headers = value
    else:
        raise ValueError("to must be an email address or a list of email addresses")
    recipients: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, address in getaddresses(headers):
        try:
            recipient, domain = normalize_recipient(address, allowed_domains)
        except ValueError:
            continue
        if recipient not in seen:
            recipients.append((recipient, domain))
            seen.add(recipient)
    return tuple(recipients)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _received_at(value: object) -> datetime:
    if not isinstance(value, str):
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("receivedAt must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def store_temp_mail_payload(
    session: AsyncSession,
    payload: Mapping[str, Any],
    *,
    allowed_domains: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> int:
    message_id = str(payload.get("id") or "").strip()
    if not message_id or len(message_id) > 255:
        raise ValueError("id must be a non-empty string no longer than 255 characters")
    recipients = parse_allowed_recipients(payload.get("to"), allowed_domains)
    if not recipients:
        return 0
    sender = _optional_text(payload.get("from")) or ""
    received_at = _received_at(payload.get("receivedAt"))
    rows = [
        {
            "id": message_id,
            "to": recipient,
            "from": sender,
            "domain": domain,
            "subject": _optional_text(payload.get("subject")),
            "raw": _optional_text(payload.get("raw")),
            "parsedText": _optional_text(payload.get("parsedText")),
            "parsedHtml": _optional_text(payload.get("parsedHtml")),
            "url": _optional_text(payload.get("url")),
            "aiExtractType": _optional_text(payload.get("aiExtractType")),
            "aiExtractResult": payload.get("aiExtractResult"),
            "aiExtractResultText": _optional_text(payload.get("aiExtractResultText")),
            "received_at": received_at,
        }
        for recipient, domain in recipients
    ]
    statement = insert(TempMailMessage).values(rows)
    statement = statement.on_conflict_do_nothing(index_elements=["id", "to"])
    result = await session.execute(statement)
    await session.commit()
    return int(result.rowcount or 0)


async def list_messages_for_recipient(
    session: AsyncSession,
    recipient: str,
    *,
    limit: int = 100,
) -> list[TempMailMessage]:
    normalized, _ = normalize_recipient(recipient)
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    result = await session.scalars(
        select(TempMailMessage)
        .where(TempMailMessage.recipient == normalized)
        .order_by(desc(TempMailMessage.received_at))
        .limit(limit)
    )
    return list(result.all())


async def latest_message_for_recipient(
    session: AsyncSession,
    recipient: str,
) -> TempMailMessage | None:
    messages = await list_messages_for_recipient(session, recipient, limit=1)
    return messages[0] if messages else None
