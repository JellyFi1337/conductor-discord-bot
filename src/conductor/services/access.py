"""Хранилище рангов и ключей: БД — источник правды, yml — посев и выгрузка."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import GuildAccessState, GuildPermission, GuildRank
from conductor.db.session import dialect_insert, session_scope
from conductor.domain.access import (
    ACTION_DEFAULTS,
    ACTION_KEYS,
    BINDABLE_RANKS,
    CONFIG_MANAGE,
    HELP_READ,
    AccessDecision,
    AccessSnapshot,
    LockoutBlocked,
    LockoutRisk,
    PermissionRule,
    Rank,
    authorize,
    empty_rank_roles,
    lockout_risk,
    parse_rank_list,
    parse_rank_token,
    rank_holding_role,
    ranks_for_storage,
    resolve_rank,
    scheme_drift_keys,
    special_locked_now,
)
from conductor.presentation import copy
from conductor.presentation.layout import event
from conductor.presentation.tokens import Tone
from conductor.services.runtime import log_tick
from conductor.services.watchers import bind_staff_roles

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.access")

SNAPSHOT_CACHE_KEY = "*"
EXAMPLE_NAME = "permissions.yml.example"

__all__ = [
    "PermissionsFile",
    "RankRoleTaken",
    "apply_scheme_defaults",
    "bind_access_jobs",
    "cached_decide",
    "cached_snapshot",
    "dump_permissions_yaml",
    "ensure_defaults",
    "example_permissions_path",
    "export_payload",
    "import_yaml",
    "load_snapshot",
    "log_fallback_admin",
    "log_limit_bypass",
    "parse_permissions_yaml",
    "restore_defaults",
    "set_key_extra_roles",
    "set_key_ranks",
    "set_rank_role",
    "sweep_orphaned_special",
    "unlock_if_orphaned",
]


class RankRoleTaken(ValueError):
    def __init__(self, owner: Rank) -> None:
        self.owner = owner
        super().__init__(owner.key)


@dataclass(frozen=True, slots=True)
class PermissionsFile:
    ranks: dict[str, tuple[int, ...]]
    actions: dict[str, tuple[str, ...]]
    extra_roles: dict[str, tuple[int, ...]] = field(default_factory=dict)
    extra_roles_section: bool = False


def example_permissions_path() -> Path:
    here = Path(__file__).resolve()
    candidates = (
        here.parents[3] / "data" / EXAMPLE_NAME,
        Path.cwd() / "data" / EXAMPLE_NAME,
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def parse_permissions_yaml(text: str) -> PermissionsFile:
    raw = text.lstrip("\ufeff").strip()
    if not raw or raw in {"{}", "---"}:
        return PermissionsFile({}, {})
    if raw.startswith("{") and raw.endswith("}"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return _from_mapping(payload)
    ranks: dict[str, tuple[int, ...]] = {}
    actions: dict[str, tuple[str, ...]] = {}
    extra_roles: dict[str, tuple[int, ...]] = {}
    extra_roles_section = False
    section: str | None = None
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip() or stripped.strip() in {"{}", "---"}:
            continue
        heading = stripped.strip()
        key_name, sep, rest = heading.partition(":")
        if not sep:
            continue
        name = key_name.strip()
        if name in {"ranks", "actions", "extra_roles"}:
            section = name
            if name == "extra_roles":
                extra_roles_section = True
            continue
        if section is None or not name:
            continue
        values = _parse_flow_list(rest.strip())
        if section == "ranks":
            ranks[name] = tuple(_as_snowflake(item) for item in values)
        elif section == "extra_roles":
            extra_roles[name] = tuple(_as_snowflake(item) for item in values)
        else:
            actions[name] = tuple(values)
    return PermissionsFile(ranks, actions, extra_roles, extra_roles_section)


def dump_permissions_yaml(payload: PermissionsFile) -> str:
    lines = ["ranks:"]
    for rank in (Rank.SPECIAL, Rank.CHIEF, Rank.SENIOR, Rank.WATCHER):
        ids = payload.ranks.get(rank.key, ())
        lines.append(f"  {rank.key}: {_format_flow(ids)}")
    lines.append("")
    lines.append("actions:")
    width = max(len(key) for key in ACTION_KEYS)
    known = {key: payload.actions.get(key, ranks_for_storage(ACTION_DEFAULTS[key])) for key in ACTION_KEYS}
    extras = {key: values for key, values in payload.actions.items() if key not in known}
    for key, values in (*known.items(), *sorted(extras.items())):
        lines.append(f"  {key.ljust(width)}: {_format_flow(values)}")
    if payload.extra_roles:
        lines.append("")
        lines.append("extra_roles:")
        extra_width = max(len(key) for key in payload.extra_roles)
        for key, role_ids in sorted(payload.extra_roles.items()):
            if not role_ids:
                continue
            lines.append(f"  {key.ljust(extra_width)}: {_format_flow(role_ids)}")
    lines.append("")
    return "\n".join(lines)


def _from_mapping(payload: dict[object, object]) -> PermissionsFile:
    ranks_raw = payload.get("ranks") if isinstance(payload.get("ranks"), dict) else {}
    actions_raw = payload.get("actions") if isinstance(payload.get("actions"), dict) else {}
    extra_raw = payload.get("extra_roles") if isinstance(payload.get("extra_roles"), dict) else {}
    ranks: dict[str, tuple[int, ...]] = {}
    if isinstance(ranks_raw, dict):
        for name, values in ranks_raw.items():
            ranks[str(name)] = tuple(_as_snowflake(item) for item in _as_list(values))
    actions: dict[str, tuple[str, ...]] = {}
    if isinstance(actions_raw, dict):
        for name, values in actions_raw.items():
            actions[str(name)] = tuple(str(item) for item in _as_list(values))
    extra_roles: dict[str, tuple[int, ...]] = {}
    if isinstance(extra_raw, dict):
        for name, values in extra_raw.items():
            extra_roles[str(name)] = tuple(_as_snowflake(item) for item in _as_list(values))
    return PermissionsFile(ranks, actions, extra_roles, extra_roles_section="extra_roles" in payload)


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return _parse_flow_list(value)
    return [value]


def _parse_flow_list(text: str) -> list[str]:
    raw = text.strip()
    if not raw or raw == "[]":
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    if not raw:
        return []
    return [piece.strip().strip("'\"") for piece in raw.split(",") if piece.strip().strip("'\"")]


def _format_flow(values: Iterable[object]) -> str:
    items = [str(item) for item in values]
    if not items:
        return "[]"
    return "[" + ", ".join(items) + "]"


def _as_snowflake(value: object) -> int:
    text = str(value).strip()
    return int(text)


def _decode_ranks(raw: str) -> frozenset[Rank]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = _parse_flow_list(raw)
    if isinstance(payload, str):
        return parse_rank_list(payload)
    if isinstance(payload, list):
        return parse_rank_list([str(item) for item in payload])
    return parse_rank_list(str(payload))


def _decode_role_ids(raw: str) -> frozenset[int]:
    if not raw or not raw.strip():
        return frozenset()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(payload, list):
        return frozenset()
    return frozenset(int(item) for item in payload)


def _encode_ranks(ranks: Iterable[Rank]) -> str:
    return json.dumps(list(ranks_for_storage(tuple(ranks))), ensure_ascii=False)


def _encode_role_ids(role_ids: Iterable[int]) -> str:
    return json.dumps([int(item) for item in role_ids])


async def ensure_defaults(session: AsyncSession, guild_id: int) -> GuildAccessState:
    stmt = (
        dialect_insert(session)(GuildAccessState)
        .values(guild_id=guild_id, special_ever_bound=False)
        .on_conflict_do_nothing(index_elements=("guild_id",))
    )
    await session.execute(stmt)
    await session.flush()
    state = await session.get(GuildAccessState, guild_id)
    if state is None:
        state = await session.scalar(select(GuildAccessState).where(GuildAccessState.guild_id == guild_id))
    if state is None:
        state = GuildAccessState(guild_id=guild_id, special_ever_bound=False)
        session.add(state)
        await session.flush()
    existing = {
        row.key
        for row in (await session.scalars(select(GuildPermission).where(GuildPermission.guild_id == guild_id))).all()
    }
    missing = [
        {
            "guild_id": guild_id,
            "key": key,
            "ranks": _encode_ranks(allowed),
            "extra_role_ids": "[]",
        }
        for key, allowed in ACTION_DEFAULTS.items()
        if key not in existing
    ]
    if missing:
        stmt = (
            dialect_insert(session)(GuildPermission)
            .values(missing)
            .on_conflict_do_nothing(index_elements=("guild_id", "key"))
        )
        await session.execute(stmt)
        await session.flush()
    return state


async def load_snapshot(session: AsyncSession, guild_id: int) -> AccessSnapshot:
    state = await ensure_defaults(session, guild_id)
    rank_roles = empty_rank_roles()
    rows = (await session.scalars(select(GuildRank).where(GuildRank.guild_id == guild_id))).all()
    grouped: dict[Rank, list[int]] = {rank: [] for rank in BINDABLE_RANKS}
    for row in rows:
        try:
            rank = parse_rank_token(row.rank)
        except ValueError:
            continue
        if rank not in grouped:
            continue
        grouped[rank].append(int(row.role_id))
    for rank, role_ids in grouped.items():
        rank_roles[rank] = tuple(role_ids)
    if rank_roles[Rank.SPECIAL] and not state.special_ever_bound:
        state.special_ever_bound = True
        await session.flush()
    rules: dict[str, PermissionRule] = {}
    permission_rows = (await session.scalars(select(GuildPermission).where(GuildPermission.guild_id == guild_id))).all()
    for row in permission_rows:
        try:
            allowed = _decode_ranks(row.ranks)
        except ValueError:
            allowed = ACTION_DEFAULTS.get(row.key, frozenset())
        rules[row.key] = PermissionRule(
            key=row.key,
            ranks=allowed,
            extra_role_ids=_decode_role_ids(row.extra_role_ids),
        )
    locked = special_locked_now(rank_roles, ever_bound=bool(state.special_ever_bound))
    return AccessSnapshot(
        rank_roles=rank_roles,
        rules=rules,
        special_locked=locked,
        special_ever_bound=bool(state.special_ever_bound),
    )


async def cached_snapshot(bot: Conductor, guild_id: int) -> AccessSnapshot:
    cached = bot.caches.permissions.get((guild_id, SNAPSHOT_CACHE_KEY))
    if isinstance(cached, AccessSnapshot):
        return cached
    async with session_scope(bot.sessions) as session:
        snapshot = await load_snapshot(session, guild_id)
    bot.caches.permissions.set((guild_id, SNAPSHOT_CACHE_KEY), snapshot)
    bind_staff_roles(bot, guild_id, snapshot)
    return snapshot


async def cached_decide(
    bot: Conductor,
    *,
    guild_id: int,
    user_id: int,
    member_role_ids: Iterable[int],
    is_administrator: bool,
    key: str = CONFIG_MANAGE,
    now: datetime | None = None,
) -> AccessDecision:
    snapshot = await cached_snapshot(bot, guild_id)
    role_ids = tuple(member_role_ids)
    rank = bot.caches.ranks.get((guild_id, user_id))
    if not isinstance(rank, Rank):
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        bot.caches.ranks.set((guild_id, user_id), rank)
    rule = snapshot.rule(key)
    probe = getattr(bot, "is_developer", None)
    is_developer = bool(probe(user_id)) if callable(probe) else False
    found = None
    suspended = False
    if not is_developer and key != HELP_READ:
        from conductor.services.watchers_report import cached_active_suspension

        found = await cached_active_suspension(bot, guild_id, user_id, now=now)
        suspended = found is not None
    decision = authorize(
        rank=rank,
        key=key,
        allowed=rule.ranks,
        extra_role_ids=rule.extra_role_ids,
        member_role_ids=role_ids,
        is_administrator=is_administrator,
        special_locked=snapshot.special_locked,
        is_developer=is_developer,
        suspended=suspended,
    )
    if decision.suspended and found is not None:
        decision = replace(
            decision,
            deny_copy=copy.watchers_suspended_deny(until=found.until, reason=found.reason),
        )
    if decision.fallback_admin and not snapshot.special_locked:
        log_fallback_admin(guild_id=guild_id, user_id=user_id, key=key)
    return decision


def log_fallback_admin(*, guild_id: int, user_id: int, key: str) -> None:
    log.warning(
        "ad_hoc_administrator",
        guild_id=guild_id,
        user_id=user_id,
        key=key,
        label=copy.UNOFFICIAL_ADMIN,
    )


def log_limit_bypass(guild_id: int, user_id: int, what: str, *, bot: Conductor) -> bool:
    """Карточка F в лог администрации. Дедуп раз в сутки на человека и вид."""
    cache = bot.caches.limit_bypass
    key = (int(guild_id), int(user_id), what)
    if cache.has(key):
        return False
    cache.set(key, True)
    config = bot.config_by_guild.get(guild_id)
    channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
    pump = getattr(bot, "log_pump", None)
    submit = getattr(pump, "submit", None)
    if callable(submit):
        submit(
            channel_id,
            view=event(
                copy.LIMIT_BYPASS_TITLE,
                copy.limit_bypass_body(user_id, what),
                tone=Tone.WARN,
                source=copy.LIMIT_BYPASS_SOURCE,
            ),
        )
    return True


async def apply_scheme_defaults(session: AsyncSession, guild_id: int) -> AccessSnapshot:
    """Записать текущие умолчания в ключи, которые от них отличаются. extra_role_ids не трогает."""
    snapshot = await load_snapshot(session, guild_id)
    drifted = scheme_drift_keys(snapshot)
    if not drifted:
        return snapshot
    for key in drifted:
        snapshot = await set_key_ranks(session, guild_id, key, ACTION_DEFAULTS[key], check_lockout=False)
    return snapshot


async def set_rank_role(session: AsyncSession, guild_id: int, rank: Rank, role_id: int) -> AccessSnapshot:
    if rank not in BINDABLE_RANKS:
        raise ValueError(rank.key)
    await ensure_defaults(session, guild_id)
    current = await load_snapshot(session, guild_id)
    holder = rank_holding_role(current.rank_roles, role_id, except_rank=rank)
    if holder is not None:
        raise RankRoleTaken(holder)
    await session.execute(delete(GuildRank).where(GuildRank.guild_id == guild_id, GuildRank.rank == rank.key))
    session.add(GuildRank(guild_id=guild_id, rank=rank.key, role_id=role_id))
    if rank is Rank.SPECIAL:
        state = await session.get(GuildAccessState, guild_id)
        if state is not None:
            state.special_ever_bound = True
    await session.flush()
    return await load_snapshot(session, guild_id)


async def set_key_ranks(
    session: AsyncSession,
    guild_id: int,
    key: str,
    ranks: Iterable[Rank],
    *,
    actor: Rank | None = None,
    check_lockout: bool = True,
) -> AccessSnapshot:
    if key not in ACTION_DEFAULTS:
        raise KeyError(key)
    await ensure_defaults(session, guild_id)
    wanted = frozenset(ranks)
    snapshot = await load_snapshot(session, guild_id)
    if check_lockout:
        risk = lockout_risk(
            key=key,
            wanted=wanted,
            actor=Rank.NONE if actor is None else actor,
            rank_roles=snapshot.rank_roles,
        )
        if risk is LockoutRisk.NO_HOLDER:
            raise LockoutBlocked(risk, key)
    row = await session.scalar(
        select(GuildPermission).where(GuildPermission.guild_id == guild_id, GuildPermission.key == key)
    )
    encoded = _encode_ranks(wanted)
    if row is None:
        session.add(GuildPermission(guild_id=guild_id, key=key, ranks=encoded, extra_role_ids="[]"))
    else:
        row.ranks = encoded
    await session.flush()
    return await load_snapshot(session, guild_id)


async def set_key_extra_roles(
    session: AsyncSession,
    guild_id: int,
    key: str,
    role_ids: Iterable[int],
) -> AccessSnapshot:
    if key not in ACTION_DEFAULTS:
        raise KeyError(key)
    await ensure_defaults(session, guild_id)
    row = await session.scalar(
        select(GuildPermission).where(GuildPermission.guild_id == guild_id, GuildPermission.key == key)
    )
    encoded = _encode_role_ids(role_ids)
    if row is None:
        session.add(
            GuildPermission(
                guild_id=guild_id,
                key=key,
                ranks=_encode_ranks(ACTION_DEFAULTS[key]),
                extra_role_ids=encoded,
            )
        )
    else:
        row.extra_role_ids = encoded
    await session.flush()
    return await load_snapshot(session, guild_id)


async def restore_defaults(
    session: AsyncSession,
    guild_id: int,
    *,
    keys: Iterable[str] | None = None,
) -> AccessSnapshot:
    wanted = tuple(ACTION_KEYS if keys is None else keys)
    snapshot = await load_snapshot(session, guild_id)
    for key in wanted:
        if key not in ACTION_DEFAULTS:
            continue
        snapshot = await set_key_ranks(session, guild_id, key, ACTION_DEFAULTS[key], check_lockout=False)
    return snapshot


async def unlock_if_orphaned(
    session: AsyncSession,
    guild_id: int,
    *,
    known_role_ids: Collection[int],
) -> bool:
    await ensure_defaults(session, guild_id)
    known = {int(item) for item in known_role_ids}
    rows = (
        await session.scalars(
            select(GuildRank).where(GuildRank.guild_id == guild_id, GuildRank.rank == Rank.SPECIAL.key)
        )
    ).all()
    orphans = [row for row in rows if int(row.role_id) not in known]
    if not orphans:
        return False
    for row in orphans:
        await session.delete(row)
    await session.flush()
    return True


async def import_yaml(session: AsyncSession, guild_id: int, text: str) -> AccessSnapshot:
    payload = parse_permissions_yaml(text)
    await ensure_defaults(session, guild_id)
    await session.execute(delete(GuildRank).where(GuildRank.guild_id == guild_id))
    special_bound = False
    for name, role_ids in payload.ranks.items():
        try:
            rank = parse_rank_token(name)
        except ValueError:
            continue
        if rank not in BINDABLE_RANKS:
            continue
        for role_id in role_ids:
            session.add(GuildRank(guild_id=guild_id, rank=rank.key, role_id=int(role_id)))
            if rank is Rank.SPECIAL:
                special_bound = True
    if special_bound:
        state = await session.get(GuildAccessState, guild_id)
        if state is not None:
            state.special_ever_bound = True
    for key, tokens in payload.actions.items():
        if key not in ACTION_DEFAULTS:
            continue
        try:
            allowed = parse_rank_list(tokens)
        except ValueError:
            continue
        row = await session.scalar(
            select(GuildPermission).where(GuildPermission.guild_id == guild_id, GuildPermission.key == key)
        )
        encoded = _encode_ranks(allowed)
        extras = _extras_for_import(payload, key, row)
        if row is None:
            session.add(GuildPermission(guild_id=guild_id, key=key, ranks=encoded, extra_role_ids=extras))
        else:
            row.ranks = encoded
            row.extra_role_ids = extras
    if payload.extra_roles_section:
        for key, role_ids in payload.extra_roles.items():
            if key not in ACTION_DEFAULTS or key in payload.actions:
                continue
            row = await session.scalar(
                select(GuildPermission).where(GuildPermission.guild_id == guild_id, GuildPermission.key == key)
            )
            encoded_extras = _encode_role_ids(role_ids)
            if row is None:
                session.add(
                    GuildPermission(
                        guild_id=guild_id,
                        key=key,
                        ranks=_encode_ranks(ACTION_DEFAULTS[key]),
                        extra_role_ids=encoded_extras,
                    )
                )
            else:
                row.extra_role_ids = encoded_extras
    await session.flush()
    return await load_snapshot(session, guild_id)


def _extras_for_import(payload: PermissionsFile, key: str, row: GuildPermission | None) -> str:
    if payload.extra_roles_section and key in payload.extra_roles:
        return _encode_role_ids(payload.extra_roles[key])
    if row is not None:
        return row.extra_role_ids
    return "[]"


def export_payload(snapshot: AccessSnapshot) -> PermissionsFile:
    ranks = {rank.key: snapshot.rank_roles.get(rank, ()) for rank in BINDABLE_RANKS}
    actions: dict[str, tuple[str, ...]] = {}
    extra_roles: dict[str, tuple[int, ...]] = {}
    for key in ACTION_KEYS:
        rule = snapshot.rule(key)
        actions[key] = ranks_for_storage(rule.ranks)
        extras = tuple(int(item) for item in rule.extra_role_ids)
        if extras:
            extra_roles[key] = extras
    return PermissionsFile(ranks, actions, extra_roles, extra_roles_section=bool(extra_roles))


def bind_access_jobs(bot: Conductor) -> None:
    scheduler = getattr(bot, "scheduler", None)
    every = getattr(scheduler, "every", None)
    if callable(every):
        every(3600, "access-unlock", lambda: _unlock_all_guilds(bot))


async def _unlock_all_guilds(bot: Conductor) -> None:
    changed = 0
    for guild in tuple(getattr(bot, "guilds", ())):
        serves = getattr(bot, "serves", None)
        if callable(serves) and not serves(getattr(guild, "id", None)):
            continue
        if await sweep_orphaned_special(bot, guild):
            changed += 1
    log_tick(log, "access_unlock", changed=changed)


async def sweep_orphaned_special(bot: Conductor, guild: object) -> bool:
    guild_id = int(getattr(guild, "id", 0) or 0)
    if not guild_id:
        return False
    known = tuple(int(role.id) for role in getattr(guild, "roles", ()) if getattr(role, "id", None))
    async with session_scope(bot.sessions) as session:
        changed = await unlock_if_orphaned(session, guild_id, known_role_ids=known)
    if not changed:
        return False
    invalidate = getattr(getattr(bot, "caches", None), "invalidate_access", None)
    if callable(invalidate):
        invalidate(guild_id)
    _notify_special_unlocked(bot, guild)
    return True


def _notify_special_unlocked(bot: Conductor, guild: object) -> None:
    guild_id = int(getattr(guild, "id", 0) or 0)
    log.warning("special_role_orphaned", guild_id=guild_id)
    config = getattr(bot, "config_by_guild", {}).get(guild_id)
    channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
    pump = getattr(bot, "log_pump", None)
    submit = getattr(pump, "submit", None)
    if callable(submit) and channel_id is not None:
        submit(
            channel_id,
            view=event(
                copy.ACCESS_UNLOCK_TITLE,
                copy.ACCESS_UNLOCK_BODY,
                tone=Tone.WARN,
                source=copy.ADMIN_LOG_SOURCE,
            ),
        )
