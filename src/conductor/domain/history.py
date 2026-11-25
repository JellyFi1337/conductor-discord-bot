from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

_URL = re.compile(r"https?://[^\s<]+", re.IGNORECASE)

TIE_WEIGHTS = {
    "reply": 3,
    "mention": 2,
    "reaction": 1,
}


class TieKind(StrEnum):
    MENTION = "mention"
    REPLY = "reply"
    REACTION = "reaction"


@dataclass(slots=True, frozen=True)
class MessageFacts:
    guild_id: int
    user_id: int
    channel_id: int
    message_id: int
    created_at: datetime
    is_bot: bool
    attachments: int
    stickers: int
    embeds: int
    links: int
    mention_ids: tuple[int, ...]
    reply_to_id: int | None
    author_name: str = ""
    display_name: str = ""


@dataclass(slots=True, frozen=True)
class PairScore:
    left_id: int
    right_id: int
    strength: int
    replies: int
    mentions: int
    reactions: int
    events: int


@dataclass(frozen=True, slots=True)
class HistoryWalk:
    after_id: int | None = None
    before_id: int | None = None


def planned_history_walks(window: tuple[int, int] | None, *, caught_up: bool) -> tuple[HistoryWalk, ...]:
    """Newer messages first; older history only until the channel start is known."""
    if window is None:
        return (HistoryWalk(),)
    oldest, newest = window
    walks = [HistoryWalk(after_id=newest)]
    if not caught_up:
        walks.append(HistoryWalk(before_id=oldest))
    return tuple(walks)


def walk_reached_boundary(fetched: int, limit: int | None) -> bool:
    if limit is None or limit <= 0:
        return True
    return fetched < limit


def count_links(content: str | None) -> int:
    if not content:
        return 0
    return len(_URL.findall(content))


def unique_user_ids(author_id: int, ids: list[int] | tuple[int, ...], *extra: int | None) -> tuple[int, ...]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in (*ids, *extra):
        if value is None or value == author_id or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def already_ingested(message_id: int, oldest: int | None, newest: int | None) -> bool:
    if oldest is None or newest is None:
        return False
    return oldest <= message_id <= newest


def emoji_key(emoji: object) -> str:
    ident = getattr(emoji, "id", None)
    if ident:
        return f"custom:{ident}"
    name = getattr(emoji, "name", None)
    if name:
        return f"unicode:{name}"
    return f"raw:{emoji}"


def extend_window(message_id: int, oldest: int | None, newest: int | None) -> tuple[int, int]:
    if oldest is None or newest is None:
        return message_id, message_id
    return min(oldest, message_id), max(newest, message_id)


def directed_ties(facts: MessageFacts) -> list[tuple[int, int, TieKind]]:
    if facts.is_bot:
        return []
    ties: list[tuple[int, int, TieKind]] = []
    for target_id in facts.mention_ids:
        ties.append((facts.user_id, target_id, TieKind.MENTION))
    if facts.reply_to_id is not None and facts.reply_to_id != facts.user_id:
        ties.append((facts.user_id, facts.reply_to_id, TieKind.REPLY))
    return ties


def pair_key(left_id: int, right_id: int) -> tuple[int, int]:
    return (left_id, right_id) if left_id < right_id else (right_id, left_id)


def score_pairs(edges: list[tuple[int, int, str, int]]) -> list[PairScore]:
    strength: dict[tuple[int, int], int] = defaultdict(int)
    replies: dict[tuple[int, int], int] = defaultdict(int)
    mentions: dict[tuple[int, int], int] = defaultdict(int)
    reactions: dict[tuple[int, int], int] = defaultdict(int)
    events: dict[tuple[int, int], int] = defaultdict(int)
    for source_id, target_id, kind, count in edges:
        if source_id == target_id or count <= 0:
            continue
        key = pair_key(source_id, target_id)
        strength[key] += count * TIE_WEIGHTS.get(kind, 1)
        events[key] += count
        if kind == TieKind.REPLY:
            replies[key] += count
        elif kind == TieKind.MENTION:
            mentions[key] += count
        elif kind == TieKind.REACTION:
            reactions[key] += count
    scores = [
        PairScore(
            left_id=left_id,
            right_id=right_id,
            strength=strength[key],
            replies=replies[key],
            mentions=mentions[key],
            reactions=reactions[key],
            events=events[key],
        )
        for key in strength
        for left_id, right_id in (key,)
    ]
    scores.sort(key=lambda item: (-item.strength, -item.events, item.left_id, item.right_id))
    return scores


def partners_for(scores: list[PairScore], user_id: int) -> list[tuple[int, PairScore]]:
    found: list[tuple[int, PairScore]] = []
    for score in scores:
        if score.left_id == user_id:
            found.append((score.right_id, score))
        elif score.right_id == user_id:
            found.append((score.left_id, score))
    return found
