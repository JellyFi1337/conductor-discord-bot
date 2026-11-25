"""Видимость записей /help по рангу. Без Discord, без БД и без текстов."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from conductor.domain.access import AccessSnapshot, Rank, can

START = "start"
SETUP = "setup"
STATS = "stats"
LOYALTY = "loyalty"
STAFF = "staff"
VOICE = "voice"
TICKETS = "tickets"
MOD = "mod"
REPUTATION = "reputation"
COMMUNITY = "community"
LIFE = "life"
DATA = "data"

SECTION_IDS: tuple[str, ...] = (
    START,
    SETUP,
    STATS,
    LOYALTY,
    STAFF,
    VOICE,
    TICKETS,
    MOD,
    REPUTATION,
    COMMUNITY,
    LIFE,
    DATA,
)

KIND_COMMAND = "command"
KIND_BUTTON = "button"
TAB_MINE = "mine"
TAB_MEMO = "memo"
TAB_START = "start"
PAGE_SIZE = 10
MEMO_PAGE_SIZE = 4
PARTICIPANT_WHO = frozenset({"ticket_author", "ticket_close", "room", "rep_show"})
OWNER_WHO = frozenset({"owner", "bot_owner"})


@dataclass(frozen=True, slots=True)
class GuideCommand:
    path: str
    key: str | None = None
    who: str | None = None
    summary: str = ""
    details: tuple[str, ...] = ()
    params: tuple[tuple[str, str], ...] = ()
    kind: str = KIND_COMMAND
    section: str = ""


def parse_help_rank(value: str) -> Rank | None:
    token = value.strip().lower()
    for rank in Rank:
        if rank is Rank.NONE:
            continue
        if rank.key == token:
            return rank
    return None


def entry_visible(
    entry: GuideCommand,
    rank: Rank,
    snapshot: AccessSnapshot,
    *,
    developer: bool = False,
    member_role_ids: Collection[int] = (),
) -> bool:
    if entry.who in OWNER_WHO:
        return developer
    if entry.who == "duty":
        return rank >= Rank.WATCHER
    if entry.key is None:
        return True
    rule = snapshot.rule(entry.key)
    if can(
        rank,
        allowed=rule.ranks,
        extra_role_ids=rule.extra_role_ids,
        member_role_ids=member_role_ids,
    ):
        return True
    return entry.who in PARTICIPANT_WHO


def visible_entries(
    snapshot: AccessSnapshot,
    rank: Rank,
    *,
    entries: Sequence[GuideCommand],
    developer: bool = False,
    member_role_ids: Collection[int] = (),
) -> tuple[GuideCommand, ...]:
    return tuple(
        item
        for item in entries
        if entry_visible(item, rank, snapshot, developer=developer, member_role_ids=member_role_ids)
    )


def memo_entries(snapshot: AccessSnapshot, entries: Sequence[GuideCommand]) -> tuple[GuideCommand, ...]:
    return visible_entries(snapshot, Rank.NONE, entries=entries)


def tab_entries(
    snapshot: AccessSnapshot,
    rank: Rank,
    tab: str,
    *,
    entries: Sequence[GuideCommand],
    developer: bool = False,
    member_role_ids: Collection[int] = (),
) -> tuple[GuideCommand, ...]:
    if tab == TAB_START:
        return ()
    if tab == TAB_MEMO:
        return memo_entries(snapshot, entries)
    return visible_entries(
        snapshot,
        rank,
        entries=entries,
        developer=developer,
        member_role_ids=member_role_ids,
    )


def page_size_for(tab: str) -> int:
    return MEMO_PAGE_SIZE if tab == TAB_MEMO else PAGE_SIZE


def paged_entries(
    entries: Sequence[GuideCommand],
    page: int,
    *,
    size: int,
) -> tuple[tuple[GuideCommand, ...], int, int]:
    total = max(1, (len(entries) + size - 1) // size) if entries else 1
    current = min(max(1, page), total)
    start = (current - 1) * size
    return tuple(entries[start : start + size]), current, total
