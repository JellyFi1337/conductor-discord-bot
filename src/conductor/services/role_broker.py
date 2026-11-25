"""Единственная точка смены ролей Discord.

Подсистемы не видят чужие роли. Заявки на одного человека сливаются
в один add_roles и один remove_roles. Частота — через очередь 1d.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import discord
import structlog

from conductor.db.models import RoleChange
from conductor.db.session import session_scope
from conductor.domain.mutations import MutationScope
from conductor.domain.roles import RoleOpError
from conductor.domain.traffic import Lane, current_lane
from conductor.presentation import copy
from conductor.presentation.layout import event
from conductor.presentation.tokens import Tone
from conductor.services.mutation_lock import role_broker_allowed
from conductor.services.old_roles import old_role_id_set
from conductor.services.queue import ActionQueue

log = structlog.get_logger("conductor.roles")

ROLE_CHANGE_INTERVAL = 600.0

__all__ = [
    "HUMAN_ROLE_SUBSYSTEMS",
    "ROLE_CHANGE_INTERVAL",
    "RoleApplyResult",
    "RoleBindOverlap",
    "RoleBroker",
    "RoleBrokerError",
    "RoleClaim",
    "RoleSubsystem",
    "combine_claims",
    "filter_owned",
    "log_role_failure",
    "net_member_roles",
    "report_skipped_roles",
    "scope_for",
]


class RoleSubsystem(StrEnum):
    LOYALTY = "loyalty"
    REPUTATION = "reputation"
    ACTIVITY = "activity"
    STAFF = "staff"
    MODERATION = "moderation"
    GIVEAWAY = "giveaway"
    VERIFICATION = "verification"
    SELFROLES = "selfroles"
    ACHIEVEMENTS = "achievements"
    OWNER = "owner"


HUMAN_ROLE_SUBSYSTEMS = frozenset({RoleSubsystem.SELFROLES, RoleSubsystem.VERIFICATION})


class RoleBrokerError(RuntimeError):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class RoleBindOverlap(RoleBrokerError):
    def __init__(self, role_id: int, owner: RoleSubsystem) -> None:
        self.role_id = int(role_id)
        self.owner = owner
        super().__init__(copy.role_overlap(self.role_id, owner.value))


@dataclass(frozen=True, slots=True)
class RoleClaim:
    guild_id: int
    user_id: int
    subsystem: RoleSubsystem
    add: tuple[int, ...] = ()
    remove: tuple[int, ...] = ()
    reason: str = ""
    urgent: bool = False
    actor_id: int | None = None


@dataclass(frozen=True, slots=True)
class RoleApplyResult:
    ok: bool
    deferred: bool = False
    called: bool = False
    missing_member: bool = False
    add: tuple[int, ...] = ()
    remove: tuple[int, ...] = ()
    dropped: tuple[int, ...] = ()
    skipped: tuple[int, ...] = ()
    error: str | None = None
    code: RoleOpError | None = None
    http: discord.HTTPException | None = None


def log_role_failure(log: Any, event: str, *, guild_id: int, user_id: int, result: RoleApplyResult) -> None:
    """Ушедший участник — debug; остальные отказы брокера — warning. Код, не копирайт."""
    code = result.code
    payload = {
        "guild_id": guild_id,
        "user_id": user_id,
        "code": None if code is None else str(code),
        "error": result.error,
    }
    if code is RoleOpError.NOT_MEMBER:
        log.debug(event, **payload)
        return
    log.warning(event, **payload)


_SCOPES: dict[RoleSubsystem, MutationScope] = {
    RoleSubsystem.LOYALTY: MutationScope.ROLES_LOYALTY,
    RoleSubsystem.REPUTATION: MutationScope.ROLES_REPUTATION,
    RoleSubsystem.ACTIVITY: MutationScope.ROLES_ACTIVITY,
    RoleSubsystem.STAFF: MutationScope.ROLES_STAFF,
    RoleSubsystem.MODERATION: MutationScope.MEMBERS_MODERATION,
    RoleSubsystem.GIVEAWAY: MutationScope.ROLES_ACTIVITY,
    RoleSubsystem.VERIFICATION: MutationScope.ROLES_VERIFICATION,
    RoleSubsystem.SELFROLES: MutationScope.ROLES_SELFROLES,
    RoleSubsystem.ACHIEVEMENTS: MutationScope.ROLES_ACHIEVEMENTS,
    RoleSubsystem.OWNER: MutationScope.ROLES_OWNER,
}


def scope_for(claim: RoleClaim) -> MutationScope:
    if claim.urgent and claim.subsystem is RoleSubsystem.LOYALTY:
        return MutationScope.ROLES_LOYALTY_ENFORCEMENT
    return _SCOPES[claim.subsystem]


def filter_owned(role_ids: Iterable[int], owned: set[int] | frozenset[int]) -> tuple[int, ...]:
    return tuple(role_id for role_id in role_ids if role_id in owned)


def net_member_roles(
    *,
    have: set[int] | frozenset[int],
    add: set[int] | frozenset[int],
    remove: set[int] | frozenset[int],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    to_add = tuple(sorted(role_id for role_id in add if role_id not in have and role_id not in remove))
    to_remove = tuple(sorted(role_id for role_id in remove if role_id in have))
    return to_add, to_remove


def report_skipped_roles(
    bot: Any,
    guild_id: int,
    skipped: Sequence[int],
    *,
    seen: set[int],
    title: str,
    hint: str,
) -> None:
    """Одна карточка в лог администрации на гильдию, пока привязку не сменят."""
    ids = tuple(dict.fromkeys(int(role_id) for role_id in skipped if role_id))
    if not ids:
        return
    key = int(guild_id)
    if key in seen:
        return
    seen.add(key)
    log.warning("role_skipped", guild_id=key, skipped=ids)
    configs = getattr(bot, "config_by_guild", None)
    config = None if configs is None else configs.get(key)
    channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
    submit = getattr(getattr(bot, "log_pump", None), "submit", None)
    if channel_id is None or not callable(submit):
        return
    submit(
        int(channel_id),
        view=event(title, copy.role_skipped_body(ids, hint=hint), tone=Tone.WARN, source=copy.ADMIN_LOG_SOURCE),
    )


def combine_claims(left: RoleClaim, right: RoleClaim) -> RoleClaim:
    add = set(left.add)
    remove = set(left.remove)
    for role_id in right.add:
        remove.discard(role_id)
        add.add(role_id)
    for role_id in right.remove:
        add.discard(role_id)
        remove.add(role_id)
    return RoleClaim(
        guild_id=right.guild_id,
        user_id=right.user_id,
        subsystem=right.subsystem,
        add=tuple(sorted(add)),
        remove=tuple(sorted(remove)),
        reason=right.reason or left.reason,
        urgent=left.urgent or right.urgent,
        actor_id=right.actor_id if right.actor_id is not None else left.actor_id,
    )


class RoleBroker:
    """Сливает заявки, проверяет области и зовёт Discord один раз на человека."""

    def __init__(
        self,
        bot: Any,
        *,
        interval: float = ROLE_CHANGE_INTERVAL,
        queue: ActionQueue | None = None,
    ) -> None:
        self._bot = bot
        self.interval = interval
        self._queue = queue if queue is not None else bot.queue
        self._owned: dict[tuple[int, RoleSubsystem], frozenset[int]] = {}
        self._pending: dict[tuple[int, int], list[RoleClaim]] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    def owner_of(
        self,
        guild_id: int,
        role_id: int,
        *,
        except_subsystem: RoleSubsystem | None = None,
    ) -> RoleSubsystem | None:
        wanted = int(role_id)
        for subsystem in RoleSubsystem:
            if subsystem is except_subsystem:
                continue
            if wanted in self.owned(guild_id, subsystem):
                return subsystem
        return None

    def bind(
        self,
        guild_id: int,
        subsystem: RoleSubsystem,
        role_ids: Iterable[int],
        *,
        strict: bool = False,
    ) -> frozenset[int]:
        wanted = frozenset(int(role_id) for role_id in role_ids if role_id)
        taken: dict[int, RoleSubsystem] = {}
        for role_id in wanted:
            owner = self.owner_of(guild_id, role_id, except_subsystem=subsystem)
            if owner is not None:
                taken[role_id] = owner
        if taken and strict:
            role_id = min(taken)
            raise RoleBindOverlap(role_id, taken[role_id])
        self._owned[(guild_id, subsystem)] = wanted - frozenset(taken)
        return frozenset(taken)

    def owned(self, guild_id: int, subsystem: RoleSubsystem) -> frozenset[int]:
        bound = self._owned.get((guild_id, subsystem), frozenset())
        if subsystem is RoleSubsystem.LOYALTY:
            configs = getattr(self._bot, "config_by_guild", None)
            config = None if configs is None else configs.get(guild_id)
            return bound | frozenset(old_role_id_set(config))
        return bound

    def merge(self, claim: RoleClaim) -> None:
        key = (claim.guild_id, claim.user_id)
        rows = self._pending.setdefault(key, [])
        for index, existing in enumerate(rows):
            if existing.subsystem is claim.subsystem:
                rows[index] = combine_claims(existing, claim)
                return
        rows.append(claim)

    def submit(self, claim: RoleClaim) -> None:
        """Поставить заявку: срочная сразу, иначе окно склейки (последнее состояние побеждает)."""
        key = (claim.guild_id, claim.user_id)
        self.merge(claim)
        delay = 0.0 if claim.urgent else self._queue.until(self._cool_key(key))
        self._queue.schedule_later(self._job_key(key), delay, lambda: self.commit(claim.guild_id, claim.user_id))

    async def apply(self, claim: RoleClaim) -> RoleApplyResult:
        """Слить с уже стоящими заявками этого человека и выполнить сейчас."""
        key = (claim.guild_id, claim.user_id)
        try:
            async with self._person_lock(key):
                self.merge(claim)
                self._queue.cancel_later(self._job_key(key))
                claims = self._pending.pop(key, [])
                result = await self._run(claims)
                self._note_observe(claim.guild_id, result)
                if result.called:
                    self._queue.touch(self._cool_key(key), self.interval)
        finally:
            self._drop_idle(key)
        return result

    async def commit(self, guild_id: int, user_id: int) -> RoleApplyResult:
        key = (guild_id, user_id)
        try:
            async with self._person_lock(key):
                claims = self._pending.pop(key, [])
                if not claims:
                    result = RoleApplyResult(ok=True)
                else:
                    result = await self._run(claims)
                    self._note_observe(guild_id, result)
                    if result.called:
                        self._queue.touch(self._cool_key(key), self.interval)
        finally:
            self._drop_idle(key)
        return result

    def _person_lock(self, key: tuple[int, int]) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _drop_idle(self, key: tuple[int, int]) -> None:
        lock = self._locks.get(key)
        if lock is None:
            return
        if lock.locked() or self._pending.get(key):
            return
        self._locks.pop(key, None)
        self._prune_idle_locks()

    def _prune_idle_locks(self) -> None:
        if len(self._locks) < 64:
            return
        for key in tuple(self._locks):
            lock = self._locks.get(key)
            if lock is None or lock.locked() or self._pending.get(key):
                continue
            self._locks.pop(key, None)

    def _note_observe(self, guild_id: int, result: RoleApplyResult) -> None:
        observe = getattr(self._bot, "observe", None)
        note = getattr(observe, "note_broker", None)
        if callable(note):
            note(int(guild_id), ok=result.http is None)

    def _cool_key(self, key: tuple[int, int]) -> tuple[str, int, int]:
        return ("roles", key[0], key[1])

    def _job_key(self, key: tuple[int, int]) -> tuple[str, int, int]:
        return ("roles-flush", key[0], key[1])

    async def _run(self, claims: Sequence[RoleClaim]) -> RoleApplyResult:
        if not claims:
            return RoleApplyResult(ok=True)
        guild_id = claims[0].guild_id
        user_id = claims[0].user_id
        lookup = getattr(self._bot, "get_guild", None)
        guild = lookup(guild_id) if callable(lookup) else None
        prepared, dropped, skipped, refused = self._prepare(claims, guild)
        if refused:
            await self._journal(refused)
        if not prepared:
            error = None if not refused else refused[0].error
            code = RoleOpError.LOCKED if refused else None
            return RoleApplyResult(ok=not refused, dropped=dropped, skipped=skipped, error=error, code=code)

        if guild is None:
            error = copy.ROLE_GUILD_GONE
            await self._journal(self._fail_rows(prepared, error))
            return RoleApplyResult(
                ok=False, dropped=dropped, skipped=skipped, error=error, code=RoleOpError.GUILD_GONE
            )

        member = guild.get_member(user_id)
        if member is None:
            error = copy.ROLE_MEMBER_GONE
            await self._journal(self._fail_rows(prepared, error))
            return RoleApplyResult(
                ok=False,
                missing_member=True,
                dropped=dropped,
                skipped=skipped,
                error=error,
                code=RoleOpError.NOT_MEMBER,
            )

        add_ids: set[int] = set()
        remove_ids: set[int] = set()
        for row in prepared:
            add_ids.update(json.loads(row.add_role_ids))
            remove_ids.update(json.loads(row.remove_role_ids))
        have = {role.id for role in getattr(member, "roles", ())}
        to_add_ids, to_remove_ids = net_member_roles(have=have, add=add_ids, remove=remove_ids)

        add_roles, add_skipped, add_error, add_code = self._resolve_roles(guild, to_add_ids)
        remove_roles, remove_skipped, remove_error, remove_code = self._resolve_roles(guild, to_remove_ids)
        skipped = tuple(dict.fromkeys((*skipped, *add_skipped, *remove_skipped)))
        error = add_error or remove_error
        code = add_code or remove_code
        if skipped and code is None:
            code = RoleOpError.MISSING_ROLE
        if error is not None:
            await self._journal(self._fail_rows(prepared, error))
            return RoleApplyResult(ok=False, dropped=dropped, skipped=skipped, error=error, code=code)

        if not add_roles and not remove_roles:
            await self._journal(prepared)
            return RoleApplyResult(ok=True, called=False, dropped=dropped, skipped=skipped)

        reason = self._discord_reason(claims)
        outbound = getattr(self._bot, "outbound", None)
        try:
            if outbound is None:
                await self._mutate(member, add_roles, remove_roles, reason, claims)
            else:
                async with outbound:
                    await self._mutate(member, add_roles, remove_roles, reason, claims)
        except discord.Forbidden as exc:
            log.warning("role_broker_http", guild_id=guild_id, user_id=user_id, error=str(exc))
            await self._journal(self._fail_rows(prepared, str(exc)))
            return RoleApplyResult(
                ok=False,
                called=True,
                add=tuple(role.id for role in add_roles),
                remove=tuple(role.id for role in remove_roles),
                dropped=dropped,
                skipped=skipped,
                error=str(exc),
                code=RoleOpError.FORBIDDEN,
                http=exc,
            )
        except discord.HTTPException as exc:
            log.warning("role_broker_http", guild_id=guild_id, user_id=user_id, error=str(exc))
            await self._journal(self._fail_rows(prepared, str(exc)))
            return RoleApplyResult(
                ok=False,
                called=True,
                add=tuple(role.id for role in add_roles),
                remove=tuple(role.id for role in remove_roles),
                dropped=dropped,
                skipped=skipped,
                error=str(exc),
                code=RoleOpError.HTTP,
                http=exc,
            )

        caches = getattr(self._bot, "caches", None)
        invalidate = getattr(caches, "invalidate_member", None)
        if callable(invalidate):
            invalidate(guild_id, user_id)
        await self._journal(prepared)
        return RoleApplyResult(
            ok=True,
            called=True,
            add=tuple(role.id for role in add_roles),
            remove=tuple(role.id for role in remove_roles),
            dropped=dropped,
            skipped=skipped,
        )

    def _prepare(
        self,
        claims: Sequence[RoleClaim],
        guild: Any | None,
    ) -> tuple[list[RoleChange], tuple[int, ...], tuple[int, ...], list[RoleChange]]:
        dropped: list[int] = []
        skipped: list[int] = []
        rows: list[RoleChange] = []
        refused: list[RoleChange] = []
        now = datetime.now(UTC)
        for claim in claims:
            if claim.subsystem is RoleSubsystem.OWNER:
                kept_add = tuple(dict.fromkeys(int(role_id) for role_id in claim.add if role_id))
                kept_remove = tuple(dict.fromkeys(int(role_id) for role_id in claim.remove if role_id))
            else:
                owned = self.owned(claim.guild_id, claim.subsystem)
                wanted = tuple(dict.fromkeys((*claim.add, *claim.remove)))
                kept_add = filter_owned(claim.add, owned)
                kept_remove = filter_owned(claim.remove, owned)
                dropped.extend(role_id for role_id in wanted if role_id not in owned)
            if guild is not None:
                missing_add = tuple(role_id for role_id in kept_add if _guild_role(guild, role_id) is None)
                missing_remove = tuple(role_id for role_id in kept_remove if _guild_role(guild, role_id) is None)
                if missing_add or missing_remove:
                    skipped.extend(missing_add)
                    skipped.extend(missing_remove)
                    gone = set(missing_add) | set(missing_remove)
                    kept_add = tuple(role_id for role_id in kept_add if role_id not in gone)
                    kept_remove = tuple(role_id for role_id in kept_remove if role_id not in gone)
            if not kept_add and not kept_remove:
                continue
            rows.append(self._row(claim, add=kept_add, remove=kept_remove, ok=True, error=None, now=now))
        return rows, tuple(dropped), tuple(dict.fromkeys(skipped)), refused

    def _resolve_roles(
        self,
        guild: Any,
        role_ids: Sequence[int],
    ) -> tuple[list[discord.Role], tuple[int, ...], str | None, RoleOpError | None]:
        me = getattr(guild, "me", None)
        top = None if me is None else getattr(me, "top_role", None)
        found: list[discord.Role] = []
        skipped: list[int] = []
        for role_id in role_ids:
            role = _guild_role(guild, role_id)
            if role is None:
                skipped.append(int(role_id))
                continue
            if bool(getattr(role, "managed", False)):
                name = getattr(role, "name", str(role_id))
                return [], (), copy.role_managed(name), RoleOpError.MANAGED
            is_integration = getattr(role, "is_integration", None)
            if callable(is_integration) and is_integration():
                name = getattr(role, "name", str(role_id))
                return [], (), copy.role_managed(name), RoleOpError.MANAGED
            if top is not None and role >= top:
                name = getattr(role, "mention", None) or getattr(role, "name", str(role_id))
                return [], (), copy.role_above(name), RoleOpError.HIERARCHY
            found.append(role)
        return found, tuple(skipped), None, None

    async def _mutate(
        self,
        member: Any,
        add_roles: Sequence[discord.Role],
        remove_roles: Sequence[discord.Role],
        reason: str,
        claims: Sequence[RoleClaim] = (),
    ) -> None:
        human = any(claim.subsystem in HUMAN_ROLE_SUBSYSTEMS for claim in claims)
        token = current_lane.set(Lane.USER if human else Lane.BACKGROUND)
        try:
            with role_broker_allowed():
                if remove_roles:
                    await member.remove_roles(*remove_roles, reason=reason)
                if add_roles:
                    await member.add_roles(*add_roles, reason=reason)
        finally:
            current_lane.reset(token)

    def _discord_reason(self, claims: Sequence[RoleClaim]) -> str:
        reasons = [claim.reason for claim in claims if claim.reason]
        if len(reasons) == 1:
            return reasons[0][:512]
        if not reasons:
            names = "+".join(sorted({claim.subsystem.value for claim in claims}))
            return f"Conductor: {names}"[:512]
        return "; ".join(dict.fromkeys(reasons))[:512]

    def _row(
        self,
        claim: RoleClaim,
        *,
        add: Sequence[int],
        remove: Sequence[int],
        ok: bool,
        error: str | None,
        now: datetime,
    ) -> RoleChange:
        return RoleChange(
            guild_id=claim.guild_id,
            user_id=claim.user_id,
            actor_id=claim.actor_id,
            subsystem=claim.subsystem.value,
            add_role_ids=json.dumps(list(add)),
            remove_role_ids=json.dumps(list(remove)),
            reason=claim.reason,
            ok=ok,
            error=error,
            created_at=now,
        )

    def _fail_rows(self, rows: Sequence[RoleChange], error: str) -> list[RoleChange]:
        failed: list[RoleChange] = []
        for row in rows:
            failed.append(
                RoleChange(
                    guild_id=row.guild_id,
                    user_id=row.user_id,
                    actor_id=row.actor_id,
                    subsystem=row.subsystem,
                    add_role_ids=row.add_role_ids,
                    remove_role_ids=row.remove_role_ids,
                    reason=row.reason,
                    ok=False,
                    error=error,
                    created_at=row.created_at,
                )
            )
        return failed

    async def _journal(self, rows: Sequence[RoleChange]) -> None:
        sessions = getattr(self._bot, "sessions", None)
        if sessions is None or not rows:
            return
        async with session_scope(sessions) as session:
            for row in rows:
                session.add(row)


def _guild_role(guild: Any, role_id: int) -> Any | None:
    getter = getattr(guild, "get_role", None)
    if not callable(getter):
        return None
    return getter(int(role_id))
