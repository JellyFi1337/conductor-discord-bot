from __future__ import annotations

from datetime import datetime

import discord

CHANNEL_TYPE_LABELS = {
    "text": "текстовый",
    "voice": "голосовой",
    "category": "категория",
    "news": "объявления",
    "stage_voice": "трибуна",
    "forum": "форум",
    "public_thread": "публичный тред",
    "private_thread": "закрытый тред",
    "news_thread": "тред объявлений",
}

FOOTER_USER = "логи участников"
FOOTER_ADMIN = "логи администрации"
FOOTER_STATS = "статистика · только чтение"
FOOTER_LOYALTY = "преданность · роли не выдаются"


def truncate(text: str, limit: int = 1000) -> str:
    text = text.replace("`", "'")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def quote_block(text: str | None, limit: int = 900) -> str:
    body = truncate((text or "").replace("```", "'''"), limit)
    if not body.strip():
        return "*пусто*"
    lines = body.splitlines() or [body]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def field_value(value: object, fallback: str = "—") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else fallback


def chunk_lines(lines: list[str], limit: int = 1000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current = [line]
            size = len(line)
            continue
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def stamp(moment: datetime | None = None, style: str = "F") -> str:
    if moment is None:
        return discord.utils.format_dt(discord.utils.utcnow(), style)
    return discord.utils.format_dt(moment, style)


def stamp_pair(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    return f"{stamp(moment, 'F')} ({stamp(moment, 'R')})"


def user_plain(user: discord.abc.User | discord.Object | None, fallback_id: int | None = None) -> str:
    if isinstance(user, discord.abc.User):
        name = getattr(user, "display_name", None) or user.name
        return f"{name} (`{user.id}`)"
    ident = fallback_id if fallback_id is not None else (user.id if user is not None else None)
    if ident is None:
        return "неизвестно"
    return f"`{ident}`"


def user_label(user: discord.abc.User | discord.Object | None, fallback_id: int | None = None) -> str:
    if isinstance(user, discord.abc.User):
        name = getattr(user, "display_name", None) or user.name
        return f"{user.mention} · **{name}**"
    ident = fallback_id if fallback_id is not None else (user.id if user is not None else None)
    if ident is None:
        return "неизвестно"
    return f"<@{ident}> · `{ident}`"


def duration_label(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs or not parts:
        parts.append(f"{secs} сек")
    return " ".join(parts)


def change_line(old: object, new: object) -> str:
    return f"{field_value(old)} → {field_value(new)}"


def channel_label(channel: discord.abc.GuildChannel | discord.Thread | None, channel_id: int | None = None) -> str:
    if channel is not None:
        mention = getattr(channel, "mention", None)
        if mention:
            return f"{mention} · `{channel.id}`"
        return f"**{channel.name}** · `{channel.id}`"
    if channel_id is not None:
        return f"<#{channel_id}> · `{channel_id}`"
    return "неизвестный канал"


def channel_type_label(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    raw = str(getattr(channel, "type", "")).replace("ChannelType.", "")
    return CHANNEL_TYPE_LABELS.get(raw, raw or "канал")


def jump_link(url: str, label: str = "перейти") -> str:
    return f"[{label}]({url})"
