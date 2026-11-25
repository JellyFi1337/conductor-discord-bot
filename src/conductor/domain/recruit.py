"""Рекрут-ссылки: активность пришедших и одна активная на человека. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from math import ceil

ACTIVE_MESSAGES = 5
PAGE_SIZE = 10


@dataclass(frozen=True, slots=True)
class RecruitLink:
    id: int
    guild_id: int
    code: str
    owner_user_id: int
    channel_id: int
    created_by: int
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by: int | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def invite_deleted(self) -> bool:
        return self.revoked_at is not None and int(self.revoked_by or 0) == 0


@dataclass(frozen=True, slots=True)
class RecruitSummary:
    joined_total: int
    active: int
    present: int


@dataclass(frozen=True, slots=True)
class RecruitRow:
    code: str
    owner_user_id: int
    joined_total: int
    active: int
    present: int
    revoked: bool
    invite_deleted: bool


@dataclass(frozen=True, slots=True)
class RecruitListing:
    rows: tuple[RecruitRow, ...]
    page: int
    pages: int


@dataclass(frozen=True, slots=True)
class RecruitCard:
    link: RecruitLink
    summary: RecruitSummary
    url: str


@dataclass(frozen=True, slots=True)
class IssuedLink:
    link: RecruitLink
    url: str
    reused: bool


def is_active(member_present: bool, message_count: int) -> bool:
    return bool(member_present) and int(message_count) > ACTIVE_MESSAGES


def invite_url(code: str) -> str:
    token = str(code).strip()
    return f"https://discord.gg/{token}" if token else ""


def parse_code(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for prefix in (
        "https://discord.gg/",
        "http://discord.gg/",
        "https://discord.com/invite/",
        "http://discord.com/invite/",
    ):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    token = text.split("?", 1)[0].strip().strip("/")
    return token or None


def channel_blocked(channel_id: int, blocked: Sequence[int]) -> bool:
    return int(channel_id) in {int(item) for item in blocked if item}


def active_link(links: Sequence[RecruitLink], owner_user_id: int) -> RecruitLink | None:
    owner = int(owner_user_id)
    for link in links:
        if int(link.owner_user_id) == owner and link.revoked_at is None:
            return link
    return None


def summarize(members: Sequence[object], code: str) -> RecruitSummary:
    token = str(code)
    matched = [row for row in members if getattr(row, "invite_code", None) == token]
    present = 0
    active = 0
    for row in matched:
        here = bool(getattr(row, "currently_member", False))
        if here:
            present += 1
        if is_active(here, int(getattr(row, "message_count", 0) or 0)):
            active += 1
    return RecruitSummary(joined_total=len(matched), active=active, present=present)


def page_slice[T](items: Sequence[T], page: int, *, size: int = PAGE_SIZE) -> tuple[tuple[T, ...], int, int]:
    total = max(1, ceil(len(items) / max(1, size)) if items else 1)
    current = min(max(1, int(page)), total)
    start = (current - 1) * size
    return tuple(items[start : start + size]), current, total
