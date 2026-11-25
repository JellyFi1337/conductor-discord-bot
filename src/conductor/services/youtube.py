"""Опрос RSS YouTube: сеть вне транзакции, пост через log_pump."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from conductor.db.models import YoutubeFeed
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.self_roles import OptionTag
from conductor.domain.youtube import (
    DEFAULT_TEMPLATE,
    YoutubeHealth,
    channel_title,
    new_since,
    parse_feed,
    render,
    valid_channel_id,
)
from conductor.presentation.mentions import role_mentions
from conductor.services.runtime import log_tick

log = structlog.get_logger("conductor.youtube")

PERIOD_SECONDS = 600.0
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
ERROR_LIMIT = 200
TITLE_LIMIT = 200
CHANNEL_MISSING = "channel missing"

__all__ = ["PERIOD_SECONDS", "YoutubeService"]


@dataclass(frozen=True, slots=True)
class FeedView:
    id: int
    guild_id: int
    yt_channel_id: str
    yt_channel_title: str
    discord_channel_id: int | None
    ping_role_id: int | None
    template: str
    last_video_id: str | None
    last_checked_at: datetime | None
    last_error: str | None
    etag: str | None


@dataclass(frozen=True, slots=True)
class _Fetched:
    xml: str | None = None
    etag: str | None = None
    not_modified: bool = False
    error: str | None = None


class YoutubeService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(PERIOD_SECONDS, "youtube-poll", self.poll_all)

    async def feeds(self, guild_id: int) -> tuple[FeedView, ...]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(YoutubeFeed).where(YoutubeFeed.guild_id == int(guild_id)).order_by(YoutubeFeed.id)
                )
            ).all()
            return tuple(_view(row) for row in rows)

    async def health(self, guild_id: int) -> YoutubeHealth:
        items = await self.feeds(guild_id)
        checked = [item.last_checked_at for item in items if item.last_checked_at is not None]
        errors = [item.last_error for item in items if item.last_error]
        return YoutubeHealth(
            feeds=len(items),
            last_error=errors[-1] if errors else None,
            last_checked_at=max(checked) if checked else None,
        )

    async def add_feed(
        self,
        guild_id: int,
        *,
        yt_channel_id: str,
        discord_channel_id: int,
        actor_id: int,
        template: str | None = None,
        ping_role_id: int | None = None,
        now: datetime | None = None,
    ) -> FeedView | str:
        channel_id = valid_channel_id(yt_channel_id)
        if channel_id is None:
            return "bad_id"
        stored = (template or "").strip() or DEFAULT_TEMPLATE
        moment = as_utc(now or datetime.now(UTC))
        try:
            async with session_scope(self._bot.sessions) as session:
                row = YoutubeFeed(
                    guild_id=int(guild_id),
                    yt_channel_id=channel_id,
                    yt_channel_title="",
                    discord_channel_id=int(discord_channel_id),
                    ping_role_id=None if ping_role_id is None else int(ping_role_id),
                    template=stored,
                    created_by=int(actor_id),
                    created_at=moment,
                )
                session.add(row)
                await session.flush()
                return _view(row)
        except IntegrityError:
            return "duplicate"

    async def remove_feed(self, guild_id: int, feed_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(YoutubeFeed, int(feed_id))
            if row is None or row.guild_id != int(guild_id):
                return False
            await session.delete(row)
        return True

    async def set_channel(self, guild_id: int, feed_id: int, channel_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(YoutubeFeed, int(feed_id))
            if row is None or row.guild_id != int(guild_id):
                return None
            before = row.discord_channel_id
            row.discord_channel_id = int(channel_id)
        return None if before is None else int(before)

    async def set_role(self, guild_id: int, feed_id: int, role_id: int | None) -> int | None:
        stored = None if role_id is None else int(role_id)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(YoutubeFeed, int(feed_id))
            if row is None or row.guild_id != int(guild_id):
                return None
            before = row.ping_role_id
            row.ping_role_id = stored
        return None if before is None else int(before)

    async def poll_all(self) -> int:
        async with session_scope(self._bot.sessions) as session:
            ids = list(await session.scalars(select(YoutubeFeed.id).order_by(YoutubeFeed.id)))
        posted = 0
        for feed_id in ids:
            posted += await self.poll_feed(int(feed_id))
        log_tick(log, "youtube_poll", changed=posted)
        return posted

    async def poll_feed(self, feed_id: int) -> int:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(YoutubeFeed, int(feed_id))
            feed = None if row is None else _view(row)
        if feed is None or feed.discord_channel_id is None:
            return 0
        now = datetime.now(UTC)
        fetched = await self._fetch(feed.yt_channel_id, feed.etag)
        if fetched.error:
            await self._save_check(feed.id, now=now, error=fetched.error)
            return 0
        if fetched.not_modified:
            await self._save_check(feed.id, now=now, etag=feed.etag)
            return 0
        xml = fetched.xml or ""
        videos = parse_feed(xml)
        title = (channel_title(xml) or feed.yt_channel_title)[:TITLE_LIMIT]
        fresh = new_since(videos, feed.last_video_id)
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        channel_getter = getattr(self._bot, "get_channel", None)
        channel = channel_getter(int(feed.discord_channel_id)) if callable(channel_getter) else None
        if fresh.videos and (channel is None or not callable(submit)):
            # Курсор не двигаем: канал вернут — видео выйдут, а не потеряются молча с зелёным статусом.
            await self._save_check(feed.id, now=now, etag=fetched.etag, title=title, error=CHANNEL_MISSING)
            return 0
        await self._save_check(
            feed.id,
            now=now,
            last_video_id=fresh.last_id if fresh.last_id is not None else feed.last_video_id,
            etag=fetched.etag,
            title=title,
        )
        if not callable(submit):
            return 0
        role_id = feed.ping_role_id or await self._tag_role(feed.guild_id)
        mention = f"<@&{int(role_id)}>" if role_id else ""
        shown = title or feed.yt_channel_id
        for video in fresh.videos:
            submit(
                int(feed.discord_channel_id),
                content=render(feed.template, video, shown, mention),
                allowed_mentions=role_mentions(role_id),
                silent=not bool(role_id),
            )
        return len(fresh.videos)

    async def _fetch(self, channel_id: str, etag: str | None) -> _Fetched:
        session = getattr(self._bot, "http_session", None)
        getter = getattr(session, "get", None)
        if not callable(getter):
            return _Fetched(error="no session")
        headers = {"User-Agent": "Conductor/0.1"}
        if etag:
            headers["If-None-Match"] = etag
        url = FEED_URL.format(channel_id=channel_id)
        try:
            async with getter(url, headers=headers) as response:
                status = int(getattr(response, "status", 0) or 0)
                header_etag = _header(response, "ETag")
                if status == 304:
                    return _Fetched(not_modified=True, etag=header_etag or etag)
                if status >= 400:
                    return _Fetched(error=f"http {status}")
                reader = getattr(response, "text", None)
                xml = await reader() if callable(reader) else ""
                return _Fetched(xml=str(xml or ""), etag=header_etag)
        except TimeoutError:
            return _Fetched(error="timeout")
        except aiohttp.ClientError as exc:
            return _Fetched(error=_clip_error(str(exc)))
        except Exception as exc:
            log.warning("youtube_fetch_failed", channel_id=channel_id, error=str(exc))
            return _Fetched(error=_clip_error(str(exc)))

    async def _save_check(
        self,
        feed_id: int,
        *,
        now: datetime,
        error: str | None = None,
        last_video_id: str | None = None,
        etag: str | None = None,
        title: str | None = None,
    ) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(YoutubeFeed, int(feed_id))
            if row is None:
                return
            row.last_checked_at = now
            row.last_error = None if error is None else _clip_error(error)
            if last_video_id is not None:
                row.last_video_id = last_video_id
            if etag is not None:
                row.etag = etag
            if title:
                row.yt_channel_title = title[:TITLE_LIMIT]

    async def _tag_role(self, guild_id: int) -> int | None:
        self_roles = getattr(self._bot, "self_roles", None)
        getter = getattr(self_roles, "role_for_tag", None)
        if not callable(getter):
            return None
        found = await getter(int(guild_id), OptionTag.YOUTUBE.value)
        return None if not found else int(found)


def _view(row: YoutubeFeed) -> FeedView:
    return FeedView(
        id=int(row.id),
        guild_id=int(row.guild_id),
        yt_channel_id=row.yt_channel_id,
        yt_channel_title=row.yt_channel_title or "",
        discord_channel_id=None if row.discord_channel_id is None else int(row.discord_channel_id),
        ping_role_id=None if row.ping_role_id is None else int(row.ping_role_id),
        template=row.template,
        last_video_id=row.last_video_id,
        last_checked_at=None if row.last_checked_at is None else as_utc(row.last_checked_at),
        last_error=row.last_error,
        etag=row.etag,
    )


def _header(response: object, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name) or getter(name.lower())
    text = str(value or "").strip()
    return text or None


def _clip_error(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= ERROR_LIMIT:
        return compact
    return compact[: ERROR_LIMIT - 1] + "…"
