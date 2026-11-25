from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import MemberRecord
from conductor.db.session import session_scope

CENSUS_BATCH = 500


@dataclass(slots=True)
class CensusResult:
    total: int
    humans: int
    bots: int
    boosters: int
    created: int
    updated: int


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


async def upsert_member(
    session: AsyncSession,
    *,
    guild_id: int,
    user_id: int,
    username: str,
    display_name: str,
    is_bot: bool,
    joined_at: datetime,
    currently_member: bool = True,
    left_at: datetime | None = None,
    account_created_at: datetime | None = None,
    booster_since: datetime | None = None,
    invite_code: str | None = None,
    seen_at: datetime | None = None,
) -> str:
    joined_at = _aware(joined_at)
    assert joined_at is not None
    stmt = select(MemberRecord).where(
        MemberRecord.guild_id == guild_id,
        MemberRecord.user_id == user_id,
    )
    record = await session.scalar(stmt)
    if record is None:
        session.add(
            MemberRecord(
                guild_id=guild_id,
                user_id=user_id,
                username=username[:40],
                display_name=display_name[:80],
                is_bot=is_bot,
                currently_member=currently_member,
                first_joined_at=joined_at,
                last_joined_at=joined_at,
                left_at=left_at,
                account_created_at=_aware(account_created_at) if account_created_at else None,
                booster_since=_aware(booster_since) if booster_since else None,
                last_seen_at=_aware(seen_at) if seen_at else joined_at,
                invite_code=invite_code,
                message_count=0,
                voice_seconds=0,
                attachment_count=0,
                sticker_count=0,
                reply_count=0,
                link_count=0,
            )
        )
        return "created"

    record.username = username[:40]
    record.display_name = display_name[:80]
    record.is_bot = is_bot
    record.currently_member = currently_member
    record.last_joined_at = joined_at
    stored_first = _aware(record.first_joined_at)
    if stored_first is None or joined_at < stored_first:
        record.first_joined_at = joined_at
    record.left_at = left_at if not currently_member else None
    if account_created_at is not None:
        record.account_created_at = _aware(account_created_at)
    record.booster_since = _aware(booster_since) if booster_since else None
    if seen_at is not None:
        record.last_seen_at = _aware(seen_at)
    if invite_code and not record.invite_code:
        record.invite_code = invite_code
    return "updated"


async def mark_left(
    session: AsyncSession,
    *,
    guild_id: int,
    user_id: int,
    left_at: datetime,
) -> None:
    stmt = select(MemberRecord).where(
        MemberRecord.guild_id == guild_id,
        MemberRecord.user_id == user_id,
    )
    record = await session.scalar(stmt)
    if record is None:
        return
    record.currently_member = False
    record.left_at = _aware(left_at)


@dataclass(frozen=True, slots=True)
class MemberSnapshot:
    user_id: int
    username: str
    display_name: str
    is_bot: bool
    joined_at: datetime
    account_created_at: datetime | None = None
    booster_since: datetime | None = None


def changed_since(record: MemberRecord | None, joined_at: datetime) -> bool:
    if record is None or not record.currently_member:
        return True
    stored = _aware(record.last_joined_at)
    incoming = _aware(joined_at)
    return stored != incoming


async def load_member_index(session: AsyncSession, guild_id: int) -> dict[int, MemberRecord]:
    rows = (await session.scalars(select(MemberRecord).where(MemberRecord.guild_id == guild_id))).all()
    return {int(row.user_id): row for row in rows}


async def mark_leavers(
    session: AsyncSession,
    *,
    guild_id: int,
    seen: set[int],
    now: datetime,
) -> int:
    stale = (
        await session.scalars(
            select(MemberRecord).where(
                MemberRecord.guild_id == guild_id,
                MemberRecord.currently_member.is_(True),
            )
        )
    ).all()
    left = 0
    for record in stale:
        if record.user_id in seen:
            continue
        record.currently_member = False
        record.left_at = now
        left += 1
    return left


async def apply_census(
    sessions: Any,
    *,
    guild_id: int,
    members: Sequence[MemberSnapshot],
    census_at: datetime | None,
    now: datetime,
) -> CensusResult:
    first = census_at is None
    async with session_scope(sessions) as session:
        index = await load_member_index(session, guild_id)
    work = (
        list(members) if first else [item for item in members if changed_since(index.get(item.user_id), item.joined_at)]
    )
    created = updated = 0
    seen = {item.user_id for item in members}
    for start in range(0, len(work), CENSUS_BATCH):
        batch = work[start : start + CENSUS_BATCH]
        async with session_scope(sessions) as session:
            for item in batch:
                action = await upsert_member(
                    session,
                    guild_id=guild_id,
                    user_id=item.user_id,
                    username=item.username,
                    display_name=item.display_name,
                    is_bot=item.is_bot,
                    joined_at=item.joined_at,
                    account_created_at=item.account_created_at,
                    booster_since=item.booster_since,
                    seen_at=now,
                )
                if action == "created":
                    created += 1
                else:
                    updated += 1
        await asyncio.sleep(0)
    async with session_scope(sessions) as session:
        await mark_leavers(session, guild_id=guild_id, seen=seen, now=now)
    return CensusResult(
        total=len(members),
        humans=sum(1 for item in members if not item.is_bot),
        bots=sum(1 for item in members if item.is_bot),
        boosters=sum(1 for item in members if not item.is_bot and item.booster_since),
        created=created,
        updated=updated,
    )
