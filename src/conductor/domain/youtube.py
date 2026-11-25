"""RSS YouTube: id канала, разбор фида, что публиковать. Без Discord и БД."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
DEFAULT_TEMPLATE = "{role} Новое видео на канале **{channel}**: **{title}**\n{url}"
MAX_NEW = 3
SKIP_SHORTS_DEFAULT = False
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

__all__ = [
    "DEFAULT_TEMPLATE",
    "MAX_NEW",
    "NewVideos",
    "SKIP_SHORTS_DEFAULT",
    "Video",
    "YoutubeHealth",
    "channel_title",
    "is_short",
    "new_since",
    "parse_feed",
    "render",
    "valid_channel_id",
]


@dataclass(frozen=True, slots=True)
class Video:
    id: str
    title: str
    url: str
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewVideos:
    videos: tuple[Video, ...]
    last_id: str | None


@dataclass(frozen=True, slots=True)
class YoutubeHealth:
    feeds: int
    last_error: str | None = None
    last_checked_at: datetime | None = None


def valid_channel_id(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if CHANNEL_ID_RE.fullmatch(text):
        return text
    return None


def is_short(url: str) -> bool:
    return "/shorts/" in url.lower()


def parse_feed(xml_text: str) -> list[Video]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    found: list[Video] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        video_id = (entry.findtext(f"{{{YT_NS}}}videoId") or "").strip()
        if not video_id:
            continue
        title = (entry.findtext(f"{{{ATOM_NS}}}title") or "").strip()
        link = entry.find(f"{{{ATOM_NS}}}link")
        href = link.get("href") if link is not None else None
        url = (href or "").strip() or WATCH_URL.format(video_id=video_id)
        found.append(
            Video(
                id=video_id,
                title=title,
                url=url,
                published_at=_parse_when(entry.findtext(f"{{{ATOM_NS}}}published")),
            )
        )
    return found


def channel_title(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    return (root.findtext(f"{{{ATOM_NS}}}title") or "").strip()


def new_since(
    videos: Sequence[Video],
    last_id: str | None,
    *,
    skip_shorts: bool = SKIP_SHORTS_DEFAULT,
) -> NewVideos:
    usable = tuple(video for video in videos if not (skip_shorts and is_short(video.url)))
    ordered = _published_order(usable)
    if not ordered:
        return NewVideos((), last_id)
    if not last_id:
        return NewVideos((), ordered[-1].id)
    after: list[Video] = []
    seen = False
    for video in ordered:
        if seen:
            after.append(video)
        elif video.id == last_id:
            seen = True
    if not seen:
        after = [video for video in ordered if video.id != last_id][-MAX_NEW:]
    clipped = tuple(after[:MAX_NEW] if seen else after)
    if not clipped:
        return NewVideos((), last_id if seen else ordered[-1].id)
    return NewVideos(clipped, clipped[-1].id)


def render(template: str, video: Video, channel_title: str, role_mention: str) -> str:
    text = template or DEFAULT_TEMPLATE
    for token, value in (
        ("{role}", role_mention),
        ("{channel}", channel_title),
        ("{title}", video.title),
        ("{url}", video.url),
    ):
        text = text.replace(token, value)
    if not role_mention:
        text = text.lstrip()
    return text.strip()


def _published_order(videos: Sequence[Video]) -> tuple[Video, ...]:
    indexed = list(enumerate(videos))
    indexed.sort(key=lambda item: (_when(item[1]), item[0]))
    return tuple(item[1] for item in indexed)


def _when(video: Video) -> datetime:
    if video.published_at is None:
        return datetime.min.replace(tzinfo=UTC)
    return video.published_at


def _parse_when(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
