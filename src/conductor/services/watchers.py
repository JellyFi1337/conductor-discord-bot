"""Состав следящих: смена, заметки, кадровые заявки на брокер."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import GuildAccessState, ModCase, UnofficialAdminNotice, WatcherDuty, WatcherNote
from conductor.db.session import session_scope
from conductor.domain.access import (
    STAFF_RANKS,
    AccessDecision,
    AccessSnapshot,
    Rank,
    StaffingBlock,
    StaffingPolicy,
    appoint_permitted,
    appoint_role_diff,
    can_appoint,
    can_dismiss,
    dismiss_role_diff,
    resolve_rank,
    staff_role_ids,
)
from conductor.domain.watchers import RosterPerson, WatcherMetrics
from conductor.presentation import copy
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Tone
from conductor.services.role_broker import RoleApplyResult, RoleClaim, RoleSubsystem

log = structlog.get_logger("conductor.watchers")

LIST_PAGE = 10

NOTE_LIMIT = 500

__all__ = [
    "LIST_PAGE",
    "NOTE_LIMIT",
    "StaffPlan",
    "WatcherNoteView",
    "WatchersService",
    "add_note",
    "bind_staff_roles",
    "cases_this_week",
    "build_roster",
    "chunk_lines",
    "clear_unofficial",
    "collect_quotas",
    "duty_ids",
    "first_unofficial_notice",
    "load_policy",
    "plan_appoint",
    "plan_dismiss",
    "quota_today_line",
    "recent_notes",
    "roster_lines",
    "roster_people",
    "save_policy",
    "set_duty",
    "should_flag_unofficial",
    "staff_claim",
]


@dataclass(frozen=True, slots=True)
class WatcherNoteView:
    author_id: int
    body: str
    created_at: datetime


async def duty_ids(session: AsyncSession, guild_id: int) -> frozenset[int]:
    rows = (await session.scalars(select(WatcherDuty.user_id).where(WatcherDuty.guild_id == guild_id))).all()
    return frozenset(int(user_id) for user_id in rows)


async def set_duty(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    on: bool,
    *,
    actor_id: int,
    now: datetime,
) -> bool:
    row = await session.get(WatcherDuty, (guild_id, user_id))
    if on:
        if row is not None:
            return False
        session.add(WatcherDuty(guild_id=guild_id, user_id=user_id, since=now, set_by=actor_id))
        await session.flush()
        return True
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def add_note(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    *,
    author_id: int,
    body: str,
    now: datetime,
) -> WatcherNoteView:
    stored = body.strip()[:NOTE_LIMIT]
    row = WatcherNote(
        guild_id=guild_id,
        user_id=user_id,
        author_id=author_id,
        body=stored,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return WatcherNoteView(author_id=author_id, body=stored, created_at=now)


async def recent_notes(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    *,
    limit: int = 3,
) -> list[WatcherNoteView]:
    rows = (
        await session.scalars(
            select(WatcherNote)
            .where(WatcherNote.guild_id == guild_id, WatcherNote.user_id == user_id)
            .order_by(WatcherNote.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [WatcherNoteView(author_id=row.author_id, body=row.body, created_at=row.created_at) for row in rows]


async def load_policy(session: AsyncSession, guild_id: int) -> StaffingPolicy:
    state = await session.get(GuildAccessState, guild_id)
    if state is None:
        return StaffingPolicy()
    return StaffingPolicy(
        senior_appoint=bool(state.senior_appoint),
        special_appoint=bool(state.special_appoint),
    )


async def save_policy(session: AsyncSession, guild_id: int, policy: StaffingPolicy) -> StaffingPolicy:
    state = await session.get(GuildAccessState, guild_id)
    if state is None:
        state = GuildAccessState(
            guild_id=guild_id,
            special_ever_bound=False,
            senior_appoint=policy.senior_appoint,
            special_appoint=policy.special_appoint,
        )
        session.add(state)
    else:
        state.senior_appoint = policy.senior_appoint
        state.special_appoint = policy.special_appoint
    await session.flush()
    return policy


async def cases_this_week(session: AsyncSession, guild_id: int, *, now: datetime | None = None) -> dict[int, int]:
    """Сколько дел завёл следящий за последние 7 суток."""
    start = (now or datetime.now(UTC)) - timedelta(days=7)
    rows = (
        await session.execute(
            select(ModCase.created_by, func.count())
            .where(
                ModCase.guild_id == guild_id,
                ModCase.created_at >= start,
                ModCase.revoked_at.is_(None),
            )
            .group_by(ModCase.created_by)
        )
    ).all()
    return {int(user_id): int(count) for user_id, count in rows}


async def first_unofficial_notice(session: AsyncSession, guild_id: int, user_id: int) -> bool:
    """True, если это первое предупреждение на этого человека."""
    row = await session.get(UnofficialAdminNotice, (guild_id, user_id))
    if row is not None:
        return False
    session.add(UnofficialAdminNotice(guild_id=guild_id, user_id=user_id, noticed_at=datetime.now(UTC)))
    await session.flush()
    return True


async def clear_unofficial(session: AsyncSession, guild_id: int, user_id: int) -> None:
    await session.execute(
        delete(UnofficialAdminNotice).where(
            UnofficialAdminNotice.guild_id == guild_id,
            UnofficialAdminNotice.user_id == user_id,
        )
    )


def bind_staff_roles(bot: Any, guild_id: int, snapshot: AccessSnapshot) -> None:
    broker = getattr(bot, "roles", None)
    bind = getattr(broker, "bind", None)
    if not callable(bind):
        return
    bind(guild_id, RoleSubsystem.STAFF, staff_role_ids(snapshot.rank_roles))


def staff_claim(
    *,
    guild_id: int,
    user_id: int,
    add: Sequence[int],
    remove: Sequence[int],
    actor_id: int,
    reason: str,
) -> RoleClaim:
    return RoleClaim(
        guild_id=guild_id,
        user_id=user_id,
        subsystem=RoleSubsystem.STAFF,
        add=tuple(int(role_id) for role_id in add),
        remove=tuple(int(role_id) for role_id in remove),
        reason=reason,
        urgent=True,
        actor_id=actor_id,
    )


@dataclass(frozen=True, slots=True)
class StaffPlan:
    add: tuple[int, ...] = ()
    remove: tuple[int, ...] = ()
    desired: Rank | None = None
    current: Rank = Rank.NONE
    need_key: bool = False
    block: StaffingBlock | None = None
    unbound: bool = False
    senior_basis: bool = False

    @property
    def ok(self) -> bool:
        return not self.need_key and self.block is None and not self.unbound


def plan_appoint(
    *,
    snapshot: AccessSnapshot,
    policy: StaffingPolicy,
    actor: Rank,
    decision: AccessDecision,
    target_role_ids: Iterable[int],
    target_is_bot: bool,
    desired: Rank,
) -> StaffPlan:
    current = resolve_rank(target_role_ids, snapshot.rank_roles)
    if not decision.developer and not appoint_permitted(decision, actor, policy):
        return StaffPlan(desired=desired, current=current, need_key=True)
    block = (
        None
        if decision.developer
        else can_appoint(
            actor,
            desired=desired,
            target=current,
            target_is_bot=target_is_bot,
            policy=policy,
        )
    )
    if block is not None:
        return StaffPlan(desired=desired, current=current, block=block)
    diff = appoint_role_diff(desired, snapshot.rank_roles, target_role_ids)
    if isinstance(diff, StaffingBlock):
        return StaffPlan(desired=desired, current=current, block=diff, unbound=True)
    return StaffPlan(
        add=diff[0],
        remove=diff[1],
        desired=desired,
        current=current,
        senior_basis=actor is Rank.SENIOR and not decision.allowed,
    )


def plan_dismiss(
    *,
    snapshot: AccessSnapshot,
    actor: Rank,
    target_role_ids: Iterable[int],
    target_is_bot: bool,
    is_developer: bool,
) -> StaffPlan:
    current = resolve_rank(target_role_ids, snapshot.rank_roles)
    block = can_dismiss(actor, target=current, target_is_bot=target_is_bot)
    if block is not None and not is_developer:
        return StaffPlan(current=current, block=block)
    add, remove = dismiss_role_diff(snapshot.rank_roles, target_role_ids)
    return StaffPlan(add=add, remove=remove, current=current)


def should_flag_unofficial(*, is_admin: bool, snapshot: AccessSnapshot, rank: Rank) -> bool:
    return bool(is_admin) and snapshot.special_locked and rank is Rank.NONE


def roster_lines(
    guild: Any,
    snapshot: AccessSnapshot,
    *,
    on_duty: frozenset[int],
    cases: dict[int, int],
    quotas: dict[int, str] | None = None,
) -> list[str]:
    grouped: dict[Rank, list[Any]] = {rank: [] for rank in reversed(STAFF_RANKS)}
    for member in guild.members:
        if member.bot:
            continue
        rank = resolve_rank((role.id for role in member.roles), snapshot.rank_roles)
        if rank is Rank.NONE:
            continue
        grouped[rank].append(member)
    lines: list[str] = []
    for rank in reversed(STAFF_RANKS):
        people = sorted(grouped[rank], key=lambda item: item.display_name.lower())
        if not people:
            continue
        lines.append(copy.watchers_heading(rank.key))
        for member in people:
            lines.append(
                copy.watchers_member_line(
                    member.mention,
                    on_duty=member.id in on_duty,
                    cases=cases.get(member.id, 0),
                    today=None if quotas is None else quotas.get(member.id),
                )
            )
    return lines


def chunk_lines(lines: Sequence[str], *, size: int = LIST_PAGE) -> list[list[str]]:
    if not lines:
        return []
    return [list(lines[index : index + size]) for index in range(0, len(lines), size)]


def quota_today_line(rank: Rank, used: object, table: object) -> str:
    limits = table.for_rank(rank)
    return copy.quota_today(
        timeouts=int(getattr(used, "timeouts", 0) or 0),
        timeout_limit=int(limits.timeouts),
        bans=int(getattr(used, "bans", 0) or 0),
        ban_limit=int(limits.bans),
    )


def roster_people(
    members: Iterable[object],
    snapshot: AccessSnapshot,
    *,
    on_duty: frozenset[int],
    quotas: Mapping[int, str],
    metrics: Mapping[int, WatcherMetrics],
) -> list[RosterPerson]:
    people: list[RosterPerson] = []
    for member in members:
        if getattr(member, "bot", False):
            continue
        rank = resolve_rank((role.id for role in getattr(member, "roles", ())), snapshot.rank_roles)
        if rank is Rank.NONE:
            continue
        user_id = int(member.id)
        people.append(
            RosterPerson(
                user_id=user_id,
                mention=member.mention,
                display_name=member.display_name,
                rank=rank,
                on_duty=user_id in on_duty,
                today=quotas.get(user_id),
                metrics=metrics.get(user_id, WatcherMetrics()),
            )
        )
    return people


async def collect_quotas(moderation: Any, guild: Any, snapshot: AccessSnapshot) -> dict[int, str]:
    used = await moderation.used_today_map(guild.id)
    table = await moderation.quota_table(guild.id)
    found: dict[int, str] = {}
    for member in getattr(guild, "members", ()) or ():
        if getattr(member, "bot", False):
            continue
        rank = resolve_rank((role.id for role in getattr(member, "roles", ())), snapshot.rank_roles)
        if rank is Rank.NONE:
            continue
        found[int(member.id)] = quota_today_line(rank, used.get(member.id), table)
    return found


async def build_roster(bot: Any, guild: Any, snapshot: AccessSnapshot) -> list[RosterPerson]:
    from conductor.services.watchers_report import active_duty_ids

    async with session_scope(bot.sessions) as session:
        on_duty = await active_duty_ids(session, guild.id)
    metrics = await bot.watchers_report.roster_metrics(guild.id)
    quotas = await collect_quotas(bot.moderation, guild, snapshot)
    return roster_people(guild.members, snapshot, on_duty=on_duty, quotas=quotas, metrics=metrics)


class WatchersService:
    """Кадровые заявки и сверка внештатных администраторов."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._swept: set[int] = set()
        self._expect: set[tuple[int, int]] = set()

    def notify_special_locked(self, guild: Any) -> None:
        self._swept.discard(guild.id)
        spawn = getattr(self._bot, "spawn", None)
        if callable(spawn):
            spawn(self.scan_unofficial(guild), name=f"watchers-scan-{guild.id}")

    def _admin_channel(self, guild_id: int) -> int | None:
        config = getattr(self._bot, "config_by_guild", {}).get(guild_id)
        return None if config is None else config.admin_log_channel_id

    def staff_log(self, guild_id: int, body: str, *, tone: Tone = Tone.OK) -> None:
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return
        submit(
            self._admin_channel(guild_id),
            view=panel(copy.WATCHERS_STAFF_LOG, body, tone=tone, footnote=copy.WATCHERS_LOG_FOOTNOTE),
        )

    async def record_staff(
        self,
        guild_id: int,
        body: str,
        *,
        actor_id: int,
        actor_rank: Rank,
        action: str,
        target: int,
        details: str | None = None,
        tone: Tone = Tone.OK,
    ) -> None:
        from conductor.services.staff_feed import emit_staff

        self.staff_log(guild_id, body, tone=tone)
        await emit_staff(
            self._bot,
            guild_id,
            actor_id=actor_id,
            actor_rank=actor_rank,
            action=action,
            target=target,
            details=details,
        )

    async def card_kwargs(self, guild: Any, user: Any, actor: Rank) -> dict[str, Any]:
        from conductor.domain.access import granting_role_ids
        from conductor.services.access import cached_snapshot
        from conductor.services.watchers_report import active_duty_ids

        snapshot = await cached_snapshot(self._bot, guild.id)
        role_ids = tuple(role.id for role in getattr(user, "roles", ()))
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        sources = granting_role_ids(role_ids, rank, snapshot.rank_roles)
        async with session_scope(self._bot.sessions) as session:
            on_duty = int(user.id) in await active_duty_ids(session, guild.id)
        packed = await self._bot.watchers_report.card_data(guild.id, user.id)
        quota = None
        if rank is not Rank.NONE:
            used = await self._bot.moderation.used_today(guild.id, user.id)
            table = await self._bot.moderation.quota_table(guild.id)
            quota = quota_today_line(rank, used, table)
        return {
            "rank": rank,
            "role": " ".join(f"<@&{role_id}>" for role_id in sources) if sources else "—",
            "on_duty": on_duty,
            "quota": quota,
            "metrics": packed.metrics,
            "notes": packed.notes,
            "actions": packed.actions,
            "actor": actor,
            "target_id": int(user.id),
            "target_is_bot": bool(getattr(user, "bot", False)),
        }

    def log_rank_change(self, member: Any, before: Rank, after: Rank) -> None:
        tone = Tone.WARN if after < before else Tone.OK
        self.staff_log(
            member.guild.id,
            copy.watchers_rank_change(
                who=member.mention,
                before=copy.rank_label(before.key),
                after=copy.rank_label(after.key),
            ),
            tone=tone,
        )

    def consume_expected(self, guild_id: int, user_id: int) -> bool:
        key = (int(guild_id), int(user_id))
        if key not in self._expect:
            return False
        self._expect.discard(key)
        return True

    async def apply_rank(
        self,
        *,
        guild_id: int,
        user_id: int,
        add: Sequence[int],
        remove: Sequence[int],
        actor_id: int,
        reason: str,
    ) -> RoleApplyResult:
        claim = staff_claim(
            guild_id=guild_id,
            user_id=user_id,
            add=add,
            remove=remove,
            actor_id=actor_id,
            reason=reason,
        )
        key = (int(guild_id), int(user_id))
        self._expect.add(key)
        result = await self._bot.roles.apply(claim)
        if not (result.ok and result.called):
            self._expect.discard(key)
        return result

    async def scan_unofficial(self, guild: Any) -> None:
        from conductor.services.access import cached_snapshot

        snapshot = await cached_snapshot(self._bot, guild.id)
        if not snapshot.special_locked or guild.id in self._swept:
            return
        self._swept.add(guild.id)
        for member in guild.members:
            if member.bot:
                continue
            await self.maybe_unofficial(member, snapshot=snapshot)

    async def maybe_unofficial(self, member: Any, *, snapshot: AccessSnapshot | None = None) -> None:
        from conductor.services.access import cached_snapshot

        if not bool(getattr(getattr(member, "guild_permissions", None), "administrator", False)):
            return
        snap = snapshot if snapshot is not None else await cached_snapshot(self._bot, member.guild.id)
        rank = resolve_rank((role.id for role in member.roles), snap.rank_roles)
        if not should_flag_unofficial(is_admin=True, snapshot=snap, rank=rank):
            return
        async with session_scope(self._bot.sessions) as session:
            first = await first_unofficial_notice(session, member.guild.id, member.id)
        if not first:
            return
        log.warning(
            "unofficial_administrator",
            guild_id=member.guild.id,
            user_id=member.id,
            label=copy.UNOFFICIAL_ADMIN,
        )
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return
        config = getattr(self._bot, "config_by_guild", {}).get(member.guild.id)
        channel_id = None if config is None else config.admin_log_channel_id
        submit(
            channel_id,
            view=panel(
                copy.WATCHERS_UNOFFICIAL,
                [copy.watchers_who_line(member.mention), copy.WATCHERS_UNOFFICIAL_BODY],
                tone=Tone.WARN,
                footnote=copy.WATCHERS_LOG_FOOTNOTE,
            ),
        )

    async def clear_member_unofficial(self, guild_id: int, user_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            await clear_unofficial(session, guild_id, user_id)
