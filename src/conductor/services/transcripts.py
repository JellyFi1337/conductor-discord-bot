"""Текстовый транскрипт канала: тикеты и доказательства."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from conductor.presentation import copy


def transcript_stamp(value: object) -> str:
    if not isinstance(value, datetime):
        return "—"
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def format_transcript_message(message: Any, *, anonymous_author_id: int | None = None) -> str:
    stamp = transcript_stamp(getattr(message, "created_at", None))
    if stamp == "—":
        stamp = ""
    source = getattr(message, "author", None)
    author_id = getattr(source, "id", None)
    if anonymous_author_id is not None and author_id == anonymous_author_id:
        author = copy.TICKET_ANON_TRANSCRIPT
    else:
        name = getattr(source, "display_name", None) or str(source or "")
        author = f"{name} ({author_id})" if author_id is not None else name
    content = getattr(message, "content", None) or ""
    edited = f" {copy.TICKET_TRANSCRIPT_EDITED}" if getattr(message, "edited_at", None) else ""
    line = f"[{stamp}] {author}: {content}{edited}".strip()
    urls = _attachment_urls(message)
    if urls:
        line = f"{line}\n" + "\n".join(urls)
    forwards = _forward_lines(message)
    if forwards:
        line = f"{line}\n" + "\n".join(forwards)
    return line


async def collect_transcript(
    channel: Any,
    *,
    header: str = "",
    anonymous_author_id: int | None = None,
) -> tuple[str, int]:
    lines: list[str] = []
    if header:
        lines.append(header)
        lines.append("")
    count = 0
    history = getattr(channel, "history", None)
    if not callable(history):
        text = "\n".join(lines).strip() or copy.TICKET_TRANSCRIPT_EMPTY
        return text, 0
    async for message in history(limit=None, oldest_first=True):
        count += 1
        lines.append(format_transcript_message(message, anonymous_author_id=anonymous_author_id))
    text = "\n".join(lines).strip() if lines else copy.TICKET_TRANSCRIPT_EMPTY
    return text, count


def _attachment_urls(source: Any) -> list[str]:
    urls: list[str] = []
    for item in getattr(source, "attachments", None) or ():
        url = getattr(item, "url", None) or getattr(item, "proxy_url", None)
        if url:
            urls.append(str(url))
    return urls


def _forward_lines(message: Any) -> list[str]:
    snaps = getattr(message, "message_snapshots", None) or getattr(message, "snapshots", None) or ()
    lines: list[str] = []
    for item in snaps:
        text = _snapshot_content(item)
        extras = _attachment_urls(item)
        if text:
            lines.append(copy.ticket_transcript_forward(text))
        lines.extend(extras)
    return lines


def _snapshot_content(item: Any) -> str:
    if isinstance(item, str):
        return item
    content = str(getattr(item, "content", None) or "")
    if content:
        return content
    nested = getattr(item, "cached_message", None)
    if nested is None:
        return ""
    return str(getattr(nested, "content", None) or "")
