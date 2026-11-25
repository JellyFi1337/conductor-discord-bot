"""Реестр страниц /admin и запись изменений в лог администрации."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import GuildConfig, GuildLayout, ModSettings, VerificationSettings, VoiceHub
from conductor.db.session import session_scope
from conductor.domain.access import (
    ACTION_KEYS,
    BINDABLE_RANKS,
    CONFIG_MANAGE,
    CONFIG_READ,
    LOYALTY_MANAGE,
    MOD_SETTINGS,
    AccessDecision,
    AccessSnapshot,
    LockoutBlocked,
    LockoutRisk,
    Rank,
    StaffingPolicy,
    decide,
    lockout_risk,
    parse_rank_list,
    parse_rank_token,
    rank_holding_role,
    ranks_for_storage,
    resolve_rank,
)
from conductor.domain.admin import (
    PAGE_GENERAL,
    PAGE_LOGS,
    PAGE_MUTE,
    PAGE_OLD,
    PAGE_PERMS,
    PAGE_RANKS,
    PAGE_STAFFING,
    ROLE_CREATE_VALUE,
    AdminCtx,
    AdminPage,
    AdminResult,
    ControlFilter,
    PageControl,
    PageSnapshot,
    ReadyIssue,
    ReadyReport,
    ReadyState,
    control_key,
    page_access_keys,
    parse_timezone,
    perm_chunks,
    usable_timezones,
)
from conductor.domain.loyalty import TIER_LABELS, LoyaltyTier
from conductor.domain.privacy import USER_LOG_KEEP_DAYS
from conductor.domain.seed import entry_by_key
from conductor.presentation import copy
from conductor.presentation.admin import DISCORD_SELECT_LIMIT
from conductor.presentation.layout import event
from conductor.presentation.tokens import Tone
from conductor.services.access import (
    RankRoleTaken,
    cached_snapshot,
    dump_permissions_yaml,
    example_permissions_path,
    export_payload,
    import_yaml,
    restore_defaults,
    set_key_extra_roles,
    set_key_ranks,
    set_rank_role,
)
from conductor.services.admin_achievements import PAGE as ACHIEVEMENTS_PAGE
from conductor.services.admin_alerts import PAGE as ALERTS_PAGE
from conductor.services.admin_automod import PAGE as AUTOMOD_PAGE
from conductor.services.admin_data import PAGE as DATA_PAGE
from conductor.services.admin_giveaway import PAGE as GIVEAWAY_PAGE
from conductor.services.admin_groups import PAGE as GROUPS_PAGE
from conductor.services.admin_ladder import PAGE as LADDER_PAGE
from conductor.services.admin_lfg import PAGE as LFG_PAGE
from conductor.services.admin_polls import PAGE as POLLS_PAGE
from conductor.services.admin_quotas import PAGE as QUOTAS_PAGE
from conductor.services.admin_recruit import PAGE as RECRUIT_PAGE
from conductor.services.admin_reputation import PAGE as REPUTATION_PAGE
from conductor.services.admin_self_roles import PAGE as SELFROLES_PAGE
from conductor.services.admin_showcase import PAGE as SHOWCASE_PAGE
from conductor.services.admin_skeleton import PAGE as SKELETON_PAGE
from conductor.services.admin_stats import PAGE as STATS_PAGE
from conductor.services.admin_tickets import PAGE as TICKETS_PAGE
from conductor.services.admin_verification import PAGE as VERIFICATION_PAGE
from conductor.services.admin_voice import PAGE as VOICE_PAGE
from conductor.services.admin_youtube import PAGE as YOUTUBE_PAGE
from conductor.services.guild_config import get_or_create_config, remember_tz, role_id_for, set_role_id
from conductor.services.role_broker import RoleSubsystem
from conductor.services.watchers import load_policy, save_policy

_PAGES: dict[str, AdminPage] = {}


def register(page: AdminPage) -> AdminPage:
    if page.id in _PAGES:
        raise ValueError(f"duplicate admin page: {page.id}")
    _PAGES[page.id] = page
    return page


def all_pages() -> tuple[AdminPage, ...]:
    return tuple(_PAGES.values())


def page_by_id(page_id: str) -> AdminPage | None:
    return _PAGES.get(page_id)


def pages_for(
    snapshot: AccessSnapshot,
    *,
    member_role_ids: Iterable[int],
    is_administrator: bool = False,
    is_developer: bool = False,
    suspended: bool = False,
) -> tuple[AdminPage, ...]:
    return tuple(
        page
        for page in _PAGES.values()
        if page_visible(
            snapshot,
            page,
            member_role_ids=member_role_ids,
            is_administrator=is_administrator,
            is_developer=is_developer,
            suspended=suspended,
        )
    )


def page_visible(
    snapshot: AccessSnapshot,
    page: AdminPage,
    *,
    member_role_ids: Iterable[int],
    is_administrator: bool = False,
    is_developer: bool = False,
    suspended: bool = False,
) -> bool:
    roles = tuple(member_role_ids)
    return any(
        decide(
            snapshot,
            key=key,
            member_role_ids=roles,
            is_administrator=is_administrator,
            is_developer=is_developer,
            suspended=suspended,
        ).allowed
        for key in page_access_keys(page)
    )


def may_use(
    snapshot: AccessSnapshot,
    page: AdminPage,
    control_id: str,
    *,
    member_role_ids: Iterable[int],
    is_administrator: bool = False,
    is_developer: bool = False,
    suspended: bool = False,
) -> AccessDecision:
    return decide(
        snapshot,
        key=control_key(page, control_id),
        member_role_ids=member_role_ids,
        is_administrator=is_administrator,
        is_developer=is_developer,
        suspended=suspended,
    )


def filter_controls(
    snapshot: AccessSnapshot,
    page: AdminPage,
    controls: Sequence[PageControl],
    *,
    member_role_ids: Iterable[int],
    is_administrator: bool = False,
    is_developer: bool = False,
    suspended: bool = False,
) -> ControlFilter:
    allowed: list[PageControl] = []
    hidden: list[tuple[str, Rank | None]] = []
    for control in controls:
        decision = may_use(
            snapshot,
            page,
            control.id,
            member_role_ids=member_role_ids,
            is_administrator=is_administrator,
            is_developer=is_developer,
            suspended=suspended,
        )
        if decision.allowed:
            allowed.append(control)
            continue
        hidden.append((control.label, decision.need))
    return ControlFilter(tuple(allowed), tuple(hidden))


def hidden_footnote(hidden: tuple[tuple[str, Rank | None], ...]) -> str | None:
    if not hidden:
        return None
    needed = [rank for _, rank in hidden if rank is not None]
    rank = min(needed) if needed else None
    label = copy.rank_label(rank.key) if rank is not None else copy.RANK_SPECIAL
    return copy.admin_hidden_actions(len(hidden), label)


def owned_pick_error(ctx: AdminCtx, role_id: int, subsystem: RoleSubsystem) -> str | None:
    broker = getattr(ctx.bot, "roles", None)
    owner_of = None if broker is None else getattr(broker, "owner_of", None)
    if not callable(owner_of):
        return None
    owner = owner_of(ctx.guild_id, int(role_id), except_subsystem=subsystem)
    if owner is None:
        return None
    return copy.config_role_taken(copy.role_subsystem_label(owner.value))


def _is_everyone(guild: object, role: object) -> bool:
    try:
        role_id = int(getattr(role, "id", 0) or 0)
    except (TypeError, ValueError):
        return False
    try:
        if role_id and role_id == int(getattr(guild, "id", 0) or 0):
            return True
    except (TypeError, ValueError):
        pass
    default = getattr(guild, "default_role", None)
    if default is not None:
        try:
            if role_id == int(getattr(default, "id", 0) or 0):
                return True
        except (TypeError, ValueError):
            pass
    check = getattr(role, "is_default", None)
    return bool(check()) if callable(check) else False


def _position(item: object) -> int:
    try:
        return int(getattr(item, "position", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _bot_top(guild: object) -> tuple[int | None, int | None]:
    me = getattr(guild, "me", None)
    top = None if me is None else getattr(me, "top_role", None)
    if top is None:
        return None, None
    try:
        role_id = int(getattr(top, "id", 0) or 0) or None
    except (TypeError, ValueError):
        role_id = None
    return role_id, _position(top) if role_id is not None else None


def _item_id(item: object) -> int | None:
    try:
        value = int(getattr(item, "id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value or None


def _item_name(item: object) -> str:
    name = str(getattr(item, "name", "") or "")
    if name:
        return name
    found = _item_id(item)
    return "" if found is None else str(found)


def _channel_type_name(channel: object) -> str:
    kind = getattr(channel, "type", None)
    name = getattr(kind, "name", None)
    if isinstance(name, str):
        return name
    return str(kind) if isinstance(kind, str) else ""


async def free_roles(
    ctx: AdminCtx,
    *,
    subsystem: RoleSubsystem,
    limit: int = DISCORD_SELECT_LIMIT,
    exclude_ids: tuple[int, ...] = (),
    hint_owned: bool = True,
) -> tuple[tuple[str, ...], ...]:
    """Роли гильдии без @everyone, managed, выше бота и чужих подсистем."""
    _ = limit
    guild = ctx.guild
    if guild is None:
        return ()
    skipped = {int(item) for item in exclude_ids}
    bot_role_id, top = _bot_top(guild)
    broker = getattr(ctx.bot, "roles", None)
    owner_of = None if broker is None else getattr(broker, "owner_of", None)
    free: list[object] = []
    owned: list[tuple[object, RoleSubsystem]] = []
    for role in list(getattr(guild, "roles", ()) or ()):
        role_id = _item_id(role)
        if role_id is None or role_id in skipped:
            continue
        if _is_everyone(guild, role) or bool(getattr(role, "managed", False)):
            continue
        if bot_role_id is not None and role_id == bot_role_id:
            continue
        if top is not None and _position(role) > top:
            continue
        owner = owner_of(ctx.guild_id, role_id, except_subsystem=subsystem) if callable(owner_of) else None
        if owner is None:
            free.append(role)
            continue
        if hint_owned:
            owned.append((role, owner))
    free.sort(key=_position, reverse=True)
    owned.sort(key=lambda pair: _position(pair[0]), reverse=True)
    rows: list[tuple[str, ...]] = [(_item_name(role), str(_item_id(role))) for role in free]
    rows.extend(
        (
            _item_name(role),
            str(_item_id(role)),
            copy.admin_role_owned(copy.role_subsystem_label(owner.value)),
        )
        for role, owner in owned
    )
    return tuple(rows)


async def free_channels(
    ctx: AdminCtx,
    *,
    parent_id: int | None,
    channel_types: tuple[str, ...] = ("voice",),
    limit: int = DISCORD_SELECT_LIMIT,
) -> tuple[tuple[str, ...], ...]:
    """Каналы внутри категории каркаса, без нативного ChannelSelect."""
    _ = limit
    guild = ctx.guild
    if guild is None or parent_id is None:
        return ()
    wanted = {name for name in channel_types if name}
    parent = int(parent_id)
    found: list[object] = []
    channels = list(getattr(guild, "channels", ()) or ())
    if not channels:
        channels = list(getattr(guild, "voice_channels", ()) or ())
    for channel in channels:
        channel_id = _item_id(channel)
        if channel_id is None:
            continue
        raw_parent = getattr(channel, "category_id", None)
        try:
            in_parent = raw_parent is not None and int(raw_parent) == parent
        except (TypeError, ValueError):
            in_parent = False
        if not in_parent:
            continue
        if wanted and _channel_type_name(channel) not in wanted:
            continue
        found.append(channel)
    found.sort(key=_position)
    return tuple((_item_name(channel), str(_item_id(channel))) for channel in found)


async def layout_channel_id(ctx: AdminCtx, key: str) -> int | None:
    async with session_scope(ctx.bot.sessions) as session:
        row = await session.scalar(
            select(GuildLayout).where(GuildLayout.guild_id == ctx.guild_id, GuildLayout.key == key)
        )
    return None if row is None else int(row.channel_id)


def ready_issue(code: str, *, fix_control: str = "", skeleton_key: str = "") -> ReadyIssue:
    return ReadyIssue(
        code,
        copy.READY_ISSUE_LABELS.get(code, code),
        fix_control=fix_control,
        skeleton_key=skeleton_key,
    )


def pack_report(
    issues: Sequence[ReadyIssue | None],
    *,
    off: bool = False,
    state: ReadyState | None = None,
    optional_key: str = "",
) -> ReadyReport:
    found = tuple(item for item in issues if item is not None)
    if state is not None:
        return ReadyReport(state, found)
    if optional_key and _optional_unused(optional_key, found):
        return ReadyReport(ReadyState.OFF)
    if off and not found:
        return ReadyReport(ReadyState.OFF)
    if not found:
        return ReadyReport(ReadyState.READY)
    return ReadyReport(ReadyState.BLOCKED, found)


def _optional_unused(skeleton_key: str, issues: tuple[ReadyIssue, ...]) -> bool:
    entry = entry_by_key(skeleton_key)
    if entry is None or entry.required:
        return False
    return not issues or all(item.code in {"unbound", "empty"} for item in issues)


def channel_issue(
    guild: Any,
    channel_id: int | None,
    *,
    fix_control: str = "",
    skeleton_key: str = "",
) -> ReadyIssue | None:
    if channel_id is None:
        return ready_issue("unbound", fix_control=fix_control, skeleton_key=skeleton_key)
    if guild is None:
        return None
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    if getter(int(channel_id)) is None:
        return ready_issue("channel_missing", fix_control=fix_control, skeleton_key=skeleton_key)
    return None


def role_issue(
    guild: Any,
    role_id: int | None,
    *,
    fix_control: str = "",
    skeleton_key: str = "",
) -> ReadyIssue | None:
    if role_id is None:
        return ready_issue("role_missing", fix_control=fix_control, skeleton_key=skeleton_key)
    if guild is None:
        return None
    getter = getattr(guild, "get_role", None)
    if not callable(getter):
        return None
    if getter(int(role_id)) is None:
        return ready_issue("role_missing", fix_control=fix_control, skeleton_key=skeleton_key)
    return None


def bot_access_issue(
    guild: Any,
    channel_id: int | None,
    *,
    need: Sequence[str],
    skeleton_key: str = "",
    fix_control: str = "",
) -> ReadyIssue | None:
    if guild is None or channel_id is None:
        return None
    channel = _guild_channel(guild, int(channel_id))
    if channel is None:
        return None
    me = getattr(guild, "me", None)
    probe = getattr(channel, "permissions_for", None)
    if me is None or not callable(probe):
        return None
    perms = probe(me)
    for name in need:
        if not bool(getattr(perms, name, False)):
            return ready_issue("bot_perms", fix_control=fix_control, skeleton_key=skeleton_key)
    return None


def member_access_issue(
    guild: Any,
    channel_id: int | None,
    *,
    roles: Sequence[Any],
    need: Sequence[str],
    skeleton_key: str = "",
    fix_control: str = "",
) -> ReadyIssue | None:
    """Роли видят канал и имеют need с учётом overwrites категории и канала."""
    if guild is None or channel_id is None:
        return None
    channel = _guild_channel(guild, int(channel_id))
    if channel is None:
        return None
    everyone = getattr(guild, "default_role", None)
    held = [item for item in roles if item is not None]
    if everyone is not None and all(getattr(item, "id", None) != getattr(everyone, "id", None) for item in held):
        held = [everyone, *held]
    category = getattr(channel, "category", None)
    if category is None:
        parent_id = getattr(channel, "category_id", None)
        if parent_id is not None:
            category = _guild_channel(guild, int(parent_id))
    for name in need:
        if not _member_has(channel, held, name, everyone=everyone, category=category):
            return ready_issue("member_perms", fix_control=fix_control, skeleton_key=skeleton_key)
    return None


async def verified_roles(ctx: AdminCtx) -> tuple[Any, ...]:
    guild = ctx.guild
    if guild is None:
        return ()
    async with session_scope(ctx.bot.sessions) as session:
        row = await session.get(VerificationSettings, ctx.guild_id)
        role_id = None if row is None else row.verified_role_id
    if not role_id:
        return ()
    getter = getattr(guild, "get_role", None)
    if not callable(getter):
        return (type("Role", (), {"id": int(role_id)})(),)
    found = getter(int(role_id))
    return () if found is None else (found,)


def _guild_channel(guild: Any, channel_id: int) -> Any:
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    return getter(int(channel_id))


def _member_has(
    channel: Any,
    roles: Sequence[Any],
    name: str,
    *,
    everyone: Any,
    category: Any,
) -> bool:
    current = _base_perm(everyone, roles, name)
    if category is not None:
        current = _apply_overwrites(current, category, roles, name, everyone=everyone)
    return _apply_overwrites(current, channel, roles, name, everyone=everyone)


def _base_perm(everyone: Any, roles: Sequence[Any], name: str) -> bool:
    current = _perm_flag(everyone, name)
    for role in roles:
        if _perm_flag(role, name):
            current = True
    return current


def _perm_flag(role: Any, name: str) -> bool:
    if role is None:
        return True
    perms = getattr(role, "permissions", None)
    if perms is None:
        return True
    return bool(getattr(perms, name, False))


def _apply_overwrites(
    current: bool,
    layer: Any,
    roles: Sequence[Any],
    name: str,
    *,
    everyone: Any,
) -> bool:
    flag = _overwrite_flag(layer, everyone, name)
    if flag is False:
        current = False
    elif flag is True:
        current = True
    denied = False
    allowed = False
    everyone_id = getattr(everyone, "id", None)
    for role in roles:
        if getattr(role, "id", None) == everyone_id:
            continue
        flag = _overwrite_flag(layer, role, name)
        if flag is False:
            denied = True
        elif flag is True:
            allowed = True
    if denied:
        current = False
    if allowed:
        current = True
    return current


def _overwrite_flag(channel: Any, target: Any, name: str) -> bool | None:
    if target is None:
        return None
    probe = getattr(channel, "overwrites_for", None)
    overwrite = None
    if callable(probe):
        overwrite = probe(target)
    else:
        raw = getattr(channel, "overwrites", None) or {}
        try:
            if target in raw:
                overwrite = raw[target]
        except TypeError:
            overwrite = None
        if overwrite is None:
            tid = getattr(target, "id", None)
            if tid in raw:
                overwrite = raw[tid]
            else:
                overwrite = next(
                    (value for key, value in raw.items() if getattr(key, "id", None) == tid),
                    None,
                )
    if overwrite is None:
        return None
    value = getattr(overwrite, name, None)
    return value if isinstance(value, bool) else None


async def role_pick_control(
    ctx: AdminCtx,
    control_id: str,
    label: str,
    *,
    subsystem: RoleSubsystem,
    exclude_ids: tuple[int, ...] = (),
    allow_create: bool = False,
    hint_owned: bool = True,
    current: str | None = None,
    min_values: int = 1,
    max_values: int = 1,
) -> PageControl:
    picked = await free_roles(
        ctx,
        subsystem=subsystem,
        exclude_ids=exclude_ids,
        hint_owned=hint_owned,
    )
    options: list[tuple[str, ...]] = list(picked)
    if allow_create:
        options.insert(0, (copy.ADMIN_ROLE_CREATE, ROLE_CREATE_VALUE))
    return PageControl(
        "role_pick",
        control_id,
        label,
        options=tuple(options),
        current=current,
        min_values=min_values,
        max_values=max_values,
        exclude_ids=exclude_ids,
        allow_create=allow_create,
        hint_owned=hint_owned,
    )


async def channel_pick_control(
    ctx: AdminCtx,
    control_id: str,
    label: str,
    *,
    parent_id: int | None,
    channel_types: tuple[str, ...] = ("voice",),
    current: str | None = None,
    min_values: int = 1,
    max_values: int = 1,
) -> PageControl:
    options = await free_channels(ctx, parent_id=parent_id, channel_types=channel_types)
    return PageControl(
        "channel_pick",
        control_id,
        label,
        options=options,
        current=current,
        min_values=min_values,
        max_values=max_values,
        channel_types=channel_types,
    )


def log_change(
    bot: Any,
    guild_id: int,
    *,
    page: str,
    field: str,
    before: str,
    after: str,
    actor_id: int,
) -> None:
    config = getattr(bot, "config_by_guild", {}).get(int(guild_id))
    channel_id = None if config is None else config.admin_log_channel_id
    pump = getattr(bot, "log_pump", None)
    submit = getattr(pump, "submit", None)
    if not callable(submit) or channel_id is None:
        return
    submit(
        channel_id,
        view=event(
            copy.ADMIN_LOG_TITLE,
            copy.admin_changed(page, field, before, after, actor_id),
            tone=Tone.INFO,
            source=copy.ADMIN_LOG_SOURCE,
        ),
    )


def _yes(value: bool) -> str:
    return copy.yn(value)


def _mention_role(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "—"


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _ranks_phrase(tokens: tuple[str, ...]) -> str:
    if copy.ALL in tokens:
        return copy.ALL
    return ", ".join(copy.rank_label(token) for token in tokens)


async def _config(session: AsyncSession, guild_id: int) -> GuildConfig:
    return await get_or_create_config(session, int(guild_id))


async def _ready_general(ctx: AdminCtx) -> ReadyReport:
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        zone = bool(row.timezone)
    if zone:
        return pack_report(())
    return pack_report([ready_issue("empty", fix_control="general.tz")])


async def _snap_general(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        zone = row.timezone or "UTC"
    zones = usable_timezones()
    options = tuple((name, name) for name in zones)
    current = zone if zone in zones else None
    return PageSnapshot(
        fields={copy.ADMIN_FIELD_TIMEZONE: zone},
        controls=(
            PageControl("select", "general.tz", copy.ADMIN_FIELD_TIMEZONE, options=options, current=current),
            PageControl("button", "general.other", copy.ADMIN_TZ_OTHER),
        ),
    )


async def _handle_general(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id == "general.other":
        return AdminResult(ok=True, modal="text", target="general.tz")
    parsed = parse_timezone(value)
    if parsed is None:
        return AdminResult(ok=False, error=copy.ADMIN_BAD_TZ)
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        before = row.timezone or "UTC"
        row.timezone = parsed
    remember_tz(ctx.guild_id, parsed)
    await ctx.bot.refresh_config(ctx.guild_id)
    return AdminResult(ok=True, field=copy.ADMIN_FIELD_TIMEZONE, before=before, after=parsed)


_LOG_COLUMNS = {
    "logs.user": ("user_log_channel_id", copy.ADMIN_FIELD_USER_LOG),
    "logs.admin": ("admin_log_channel_id", copy.ADMIN_FIELD_ADMIN_LOG),
    "logs.staff": ("staff_feed_channel_id", copy.ADMIN_FIELD_STAFF_FEED),
}


async def _ready_logs(ctx: AdminCtx) -> ReadyReport:
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        user_log = row.user_log_channel_id
        admin_log = row.admin_log_channel_id
        staff_feed = row.staff_feed_channel_id
    core = [
        channel_issue(ctx.guild, user_log, fix_control="logs.user", skeleton_key="user_logs"),
        channel_issue(ctx.guild, admin_log, fix_control="logs.admin", skeleton_key="admin_logs"),
    ]
    extra = channel_issue(ctx.guild, staff_feed, fix_control="logs.staff", skeleton_key="staff_feed")
    found_core = tuple(item for item in core if item is not None)
    if found_core:
        return pack_report((*found_core, extra))
    if extra is not None:
        return pack_report([extra], state=ReadyState.PARTIAL)
    return pack_report(())


async def _snap_logs(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        user_log = row.user_log_channel_id
        admin_log = row.admin_log_channel_id
        staff_feed = row.staff_feed_channel_id
        keep_days = int(row.user_log_keep_days if row.user_log_keep_days is not None else USER_LOG_KEEP_DAYS)
    return PageSnapshot(
        fields={
            copy.ADMIN_FIELD_USER_LOG: _mention_channel(user_log),
            copy.ADMIN_FIELD_ADMIN_LOG: _mention_channel(admin_log),
            copy.ADMIN_FIELD_STAFF_FEED: _mention_channel(staff_feed),
            copy.ADMIN_FIELD_USER_LOG_KEEP: str(keep_days),
        },
        controls=(
            PageControl("channel", "logs.user", copy.ADMIN_FIELD_USER_LOG),
            PageControl("channel", "logs.admin", copy.ADMIN_FIELD_ADMIN_LOG),
            PageControl("channel", "logs.staff", copy.ADMIN_FIELD_STAFF_FEED),
            PageControl("button", "logs.keep", copy.ADMIN_LOGS_KEEP_BTN),
        ),
    )


async def _handle_logs(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id == "logs.keep":
        async with session_scope(ctx.bot.sessions) as session:
            row = await _config(session, ctx.guild_id)
            keep_days = int(row.user_log_keep_days if row.user_log_keep_days is not None else USER_LOG_KEEP_DAYS)
        return AdminResult(
            ok=True,
            modal="number",
            target="logs.keep.set",
            lo=0,
            hi=3_650,
            defaults=(str(keep_days),),
        )
    if control_id == "logs.keep.set":
        try:
            keep_days = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER)
        if keep_days < 0 or keep_days > 3_650:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER)
        async with session_scope(ctx.bot.sessions) as session:
            row = await _config(session, ctx.guild_id)
            before = int(row.user_log_keep_days if row.user_log_keep_days is not None else USER_LOG_KEEP_DAYS)
            row.user_log_keep_days = keep_days
        await ctx.bot.refresh_config(ctx.guild_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_FIELD_USER_LOG_KEEP,
            before=str(before),
            after=str(keep_days),
        )
    try:
        channel_id = int(value)
    except (TypeError, ValueError):
        return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
    column, field = _LOG_COLUMNS.get(control_id, (None, None))
    if column is None or field is None:
        return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
    async with session_scope(ctx.bot.sessions) as session:
        hub = await session.get(VoiceHub, ctx.guild_id)
        if hub is not None and int(hub.channel_id) == channel_id:
            return AdminResult(ok=False, error=copy.ADMIN_LOG_IS_HUB)
        row = await _config(session, ctx.guild_id)
        before_id = getattr(row, column)
        setattr(row, column, channel_id)
    await ctx.bot.refresh_config(ctx.guild_id)
    return AdminResult(
        ok=True,
        field=field,
        before=_mention_channel(before_id),
        after=_mention_channel(channel_id),
    )


async def _ready_ranks(ctx: AdminCtx) -> ReadyReport:
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    issues = [
        role_issue(
            ctx.guild,
            next(iter(snapshot.rank_roles.get(rank, ())), None),
            fix_control=f"ranks.{rank.key}",
            skeleton_key=f"role_{rank.key}",
        )
        for rank in BINDABLE_RANKS
    ]
    return pack_report(issues)


async def _snap_ranks(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    bound = {rank: next(iter(snapshot.rank_roles.get(rank, ())), None) for rank in BINDABLE_RANKS}
    fields = {copy.rank_label(rank.key): _mention_role(bound[rank]) for rank in BINDABLE_RANKS}
    controls: list[PageControl] = []
    for rank in BINDABLE_RANKS:
        others = tuple(int(role_id) for other, role_id in bound.items() if other is not rank and role_id)
        current = bound[rank]
        controls.append(
            await role_pick_control(
                ctx,
                f"ranks.{rank.key}",
                copy.rank_label(rank.key),
                subsystem=RoleSubsystem.STAFF,
                exclude_ids=others,
                current=None if current is None else str(int(current)),
            )
        )
    return PageSnapshot(fields=fields, controls=tuple(controls))


async def _handle_ranks(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    token = control_id.removeprefix("ranks.")
    rank = parse_rank_token(token)
    try:
        role_id = int(value)
    except (TypeError, ValueError):
        return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    before = _mention_role(next(iter(snapshot.rank_roles.get(rank, ())), None))
    holder = rank_holding_role(snapshot.rank_roles, role_id, except_rank=rank)
    if holder is not None:
        return AdminResult(ok=False, error=copy.config_role_taken(copy.rank_label(holder.key)))
    owner = ctx.bot.roles.owner_of(ctx.guild_id, role_id, except_subsystem=RoleSubsystem.STAFF)
    if owner is not None:
        return AdminResult(ok=False, error=copy.config_role_taken(copy.role_subsystem_label(owner.value)))
    try:
        async with session_scope(ctx.bot.sessions) as session:
            await set_rank_role(session, ctx.guild_id, rank, role_id)
    except RankRoleTaken as taken:
        return AdminResult(ok=False, error=copy.config_role_taken(copy.rank_label(taken.owner.key)))
    ctx.bot.caches.invalidate_access(ctx.guild_id)
    if rank is Rank.SPECIAL:
        notify = getattr(ctx.bot.watchers, "notify_special_locked", None)
        if callable(notify) and ctx.guild is not None:
            notify(ctx.guild)
    return AdminResult(ok=True, field=copy.rank_label(rank.key), before=before, after=_mention_role(role_id))


async def _ready_perms(_ctx: AdminCtx) -> ReadyReport:
    return pack_report(())


async def _snap_perms(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    chunks = perm_chunks(ACTION_KEYS)
    pages = max(1, len(chunks))
    current = min(max(1, leaf), pages)
    chunk = chunks[current - 1] if chunks else ()
    fields = {}
    for key in chunk:
        extras = tuple(snapshot.rule(key).extra_role_ids)
        phrase = _ranks_phrase(ranks_for_storage(snapshot.rule(key).ranks))
        if extras:
            fields[key] = f"{phrase} · {copy.config_role_mentions(list(extras))}"
        else:
            fields[key] = phrase
    controls: list[PageControl] = []
    if current > 1:
        controls.append(PageControl("button", f"perms.prev:{current - 1}", copy.ADMIN_PREV))
    if current < pages:
        controls.append(PageControl("button", f"perms.next:{current + 1}", copy.ADMIN_NEXT))
    controls.append(PageControl("button", f"perms.edit:{current}", copy.ADMIN_EDIT))
    controls.append(PageControl("button", f"perms.import:{current}", copy.ADMIN_IMPORT_EXAMPLE))
    controls.append(PageControl("button", f"perms.export:{current}", copy.ADMIN_EXPORT))
    controls.append(PageControl("button", f"perms.restore:{current}", copy.ADMIN_RESTORE_DEFAULTS))
    bound = tuple(role_id for role_ids in snapshot.rank_roles.values() for role_id in role_ids)
    for key in chunk:
        extras = tuple(int(item) for item in snapshot.rule(key).extra_role_ids)
        controls.append(
            await role_pick_control(
                ctx,
                f"perms.extra:{current}:{key}",
                f"{copy.CONFIG_EXTRA_FIELD} · {key}",
                subsystem=RoleSubsystem.STAFF,
                exclude_ids=bound,
                current=",".join(str(item) for item in extras) if extras else None,
                min_values=0,
                max_values=25,
            )
        )
    return PageSnapshot(
        fields=fields,
        controls=tuple(controls),
        footnote=copy.listing_page(current, pages) if pages > 1 else None,
        leaf=current,
    )


def _leaf_of(control_id: str, default: int = 1) -> int:
    if ":" not in control_id:
        return default
    try:
        return max(1, int(control_id.rsplit(":", 1)[1]))
    except ValueError:
        return default


async def _handle_perms(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    leaf = _leaf_of(control_id)
    if control_id.startswith("perms.prev") or control_id.startswith("perms.next"):
        return AdminResult(ok=True, leaf=leaf)
    if control_id.startswith("perms.edit"):
        return AdminResult(ok=True, modal="key_ranks", target="perms.set", leaf=leaf)
    if control_id.startswith("perms.export"):
        snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
        text = dump_permissions_yaml(export_payload(snapshot))
        return AdminResult(ok=True, file=text.encode("utf-8"), filename="permissions.yml", leaf=leaf)
    if control_id.startswith("perms.import"):
        path = example_permissions_path()
        if not path.is_file():
            return AdminResult(ok=False, error=copy.CONFIG_NO_FILE, leaf=leaf)
        raw = path.read_text(encoding="utf-8")
        async with session_scope(ctx.bot.sessions) as session:
            snapshot = await import_yaml(session, ctx.guild_id, raw)
        ctx.bot.caches.invalidate_access(ctx.guild_id)
        if snapshot.special_locked and ctx.guild is not None:
            notify = getattr(ctx.bot.watchers, "notify_special_locked", None)
            if callable(notify):
                notify(ctx.guild)
        return AdminResult(ok=True, field=copy.ADMIN_FIELD_PERMS, before="—", after=copy.CONFIG_IMPORTED, leaf=leaf)
    if control_id.startswith("perms.restore") and not control_id.startswith("perms.restore.yes"):
        return AdminResult(
            ok=True,
            confirm="perms.restore.yes",
            title=copy.ADMIN_RESTORE_CONFIRM,
            after=str(leaf),
            labels=(copy.ADMIN_RESTORE_VERB,),
            leaf=leaf,
        )
    if control_id == "perms.restore.yes":
        try:
            leaf = max(1, int(value))
        except (TypeError, ValueError):
            leaf = 1
        async with session_scope(ctx.bot.sessions) as session:
            await restore_defaults(session, ctx.guild_id)
        ctx.bot.caches.invalidate_access(ctx.guild_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_FIELD_PERMS,
            before="—",
            after=copy.ADMIN_RESTORE_DEFAULTS,
            leaf=leaf,
        )
    if control_id.startswith("perms.extra:"):
        parts = control_id.split(":", 2)
        try:
            leaf = max(1, int(parts[1]))
        except (IndexError, ValueError, TypeError):
            leaf = 1
        key = parts[2] if len(parts) > 2 else ""
        if key not in ACTION_KEYS:
            return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=leaf)
        chosen = tuple(int(item) for item in value.replace(";", ",").split(",") if item.strip().isdigit())
        async with session_scope(ctx.bot.sessions) as session:
            stored = await set_key_extra_roles(session, ctx.guild_id, key, chosen)
        ctx.bot.caches.invalidate_access(ctx.guild_id)
        extras = tuple(stored.rule(key).extra_role_ids)
        return AdminResult(
            ok=True,
            field=f"{copy.CONFIG_EXTRA_FIELD} · {key}",
            before="—",
            after=copy.config_role_mentions(list(extras)),
            leaf=leaf,
        )
    if control_id not in {"perms.set", "perms.set.yes"}:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=leaf)
    key, _, ranks = value.partition("\n")
    key = key.strip()
    if key not in ACTION_KEYS:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=leaf)
    try:
        allowed = parse_rank_list(ranks)
    except ValueError:
        return AdminResult(ok=False, error=copy.CONFIG_BAD_RANKS, leaf=leaf)
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    actor = Rank.SPECIAL if ctx.is_developer else resolve_rank(ctx.member_role_ids, snapshot.rank_roles)
    risk = lockout_risk(key=key, wanted=allowed, actor=actor, rank_roles=snapshot.rank_roles)
    if risk is LockoutRisk.NO_HOLDER:
        return AdminResult(ok=False, error=copy.lockout_no_holder(key), leaf=leaf)
    if control_id == "perms.set" and risk in {LockoutRisk.SELF_LOCKOUT, LockoutRisk.GOVERN_KEY}:
        question = copy.LOCKOUT_SELF if risk is LockoutRisk.SELF_LOCKOUT else copy.LOCKOUT_GOVERN
        return AdminResult(
            ok=True,
            confirm="perms.set.yes",
            title=question,
            after=value,
            labels=(copy.LOCKOUT_CONFIRM_VERB,),
            leaf=leaf,
        )
    before = _ranks_phrase(ranks_for_storage(snapshot.rule(key).ranks))
    try:
        async with session_scope(ctx.bot.sessions) as session:
            stored = await set_key_ranks(session, ctx.guild_id, key, allowed, actor=actor)
    except LockoutBlocked:
        return AdminResult(ok=False, error=copy.lockout_no_holder(key), leaf=leaf)
    ctx.bot.caches.invalidate_access(ctx.guild_id)
    after = _ranks_phrase(ranks_for_storage(stored.rule(key).ranks))
    return AdminResult(ok=True, field=key, before=before, after=after, leaf=leaf)


async def _ready_staffing(_ctx: AdminCtx) -> ReadyReport:
    return pack_report(())


async def _snap_staffing(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    async with session_scope(ctx.bot.sessions) as session:
        policy = await load_policy(session, ctx.guild_id)
    return PageSnapshot(
        fields={
            copy.ADMIN_FIELD_SENIOR_APPOINT: policy.senior_appoint,
            copy.ADMIN_FIELD_SPECIAL_APPOINT: policy.special_appoint,
        },
        controls=(
            PageControl(
                "button",
                "staffing.senior",
                copy.admin_toggle(policy.senior_appoint, copy.ADMIN_SENIOR_APPOINT_SUBJECT),
            ),
            PageControl(
                "button",
                "staffing.special",
                copy.admin_toggle(policy.special_appoint, copy.ADMIN_SPECIAL_APPOINT_SUBJECT),
            ),
        ),
    )


async def _handle_staffing(ctx: AdminCtx, control_id: str, _value: str) -> AdminResult:
    async with session_scope(ctx.bot.sessions) as session:
        current = await load_policy(session, ctx.guild_id)
        if control_id == "staffing.senior":
            field = copy.ADMIN_FIELD_SENIOR_APPOINT
            before = current.senior_appoint
            stored = await save_policy(
                session,
                ctx.guild_id,
                StaffingPolicy(senior_appoint=not current.senior_appoint, special_appoint=current.special_appoint),
            )
            after = stored.senior_appoint
        else:
            field = copy.ADMIN_FIELD_SPECIAL_APPOINT
            before = current.special_appoint
            stored = await save_policy(
                session,
                ctx.guild_id,
                StaffingPolicy(senior_appoint=current.senior_appoint, special_appoint=not current.special_appoint),
            )
            after = stored.special_appoint
    ctx.bot.caches.invalidate_access(ctx.guild_id)
    return AdminResult(ok=True, field=field, before=_yes(before), after=_yes(after))


async def _ready_mute(ctx: AdminCtx) -> ReadyReport:
    async with session_scope(ctx.bot.sessions) as session:
        row = await session.get(ModSettings, ctx.guild_id)
        role_id = None if row is None else row.mute_role_id
        category_id = None if row is None else row.evidence_category_id
        archive_id = None if row is None else row.evidence_archive_channel_id
    core = role_issue(ctx.guild, role_id, fix_control="mute.role", skeleton_key="role_mute")
    extra = None
    health = getattr(getattr(ctx.bot, "evidence", None), "health", None)
    if callable(health):
        denial, _opened, _overdue = await health(ctx.guild_id, guild=ctx.guild)
        extra = _mute_evidence_issue(denial)
    else:
        extra = channel_issue(
            ctx.guild,
            category_id,
            fix_control="mute.evidence_category",
            skeleton_key="cat_evidence",
        )
    archive = channel_issue(
        ctx.guild,
        archive_id,
        fix_control="mute.evidence_archive",
        skeleton_key="evidence_archive",
    )
    if core is not None:
        return pack_report([core, extra, archive])
    if extra is not None:
        return pack_report([extra, archive], state=ReadyState.PARTIAL)
    if archive is not None:
        return pack_report([archive], state=ReadyState.PARTIAL)
    return pack_report(())


def _mute_evidence_issue(denial: object | None) -> ReadyIssue | None:
    if denial is None:
        return None
    code = str(getattr(denial, "value", denial) or "")
    if code == "no_category":
        return ready_issue("unbound", fix_control="mute.evidence_category", skeleton_key="cat_evidence")
    if code in {"category_gone", "not_a_category"}:
        return ready_issue("channel_missing", fix_control="mute.evidence_category", skeleton_key="cat_evidence")
    if code == "bot_perms":
        return ready_issue("bot_perms", fix_control="mute.evidence_category", skeleton_key="cat_evidence")
    return None


async def _snap_mute(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    async with session_scope(ctx.bot.sessions) as session:
        row = await session.get(ModSettings, ctx.guild_id)
        role_id = None if row is None else row.mute_role_id
        category_id = None if row is None else row.evidence_category_id
        archive_id = None if row is None else row.evidence_archive_channel_id
        hours = 24 if row is None else int(row.evidence_remind_hours or 24)
    return PageSnapshot(
        fields={
            copy.ADMIN_FIELD_MUTE: _mention_role(role_id),
            copy.ADMIN_FIELD_EVIDENCE_CATEGORY: _mention_channel(category_id),
            copy.ADMIN_FIELD_EVIDENCE_ARCHIVE: _mention_channel(archive_id),
            copy.ADMIN_FIELD_EVIDENCE_HOURS: str(hours),
        },
        controls=(
            await role_pick_control(
                ctx,
                "mute.role",
                copy.ADMIN_FIELD_MUTE,
                subsystem=RoleSubsystem.MODERATION,
                current=None if role_id is None else str(int(role_id)),
            ),
            PageControl(
                "channel",
                "mute.evidence_category",
                copy.ADMIN_FIELD_EVIDENCE_CATEGORY,
                channel_types=("category",),
            ),
            PageControl(
                "channel",
                "mute.evidence_archive",
                copy.ADMIN_FIELD_EVIDENCE_ARCHIVE,
                channel_types=("text",),
            ),
            PageControl("button", "mute.evidence_hours", copy.ADMIN_EVIDENCE_HOURS_BTN),
        ),
        footnote=copy.ADMIN_EVIDENCE_HINT,
    )


async def _handle_mute(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id == "mute.evidence_hours":
        async with session_scope(ctx.bot.sessions) as session:
            row = await session.get(ModSettings, ctx.guild_id)
            hours = 24 if row is None else int(row.evidence_remind_hours or 24)
        return AdminResult(
            ok=True,
            modal="number",
            target="mute.evidence_hours.set",
            lo=1,
            hi=168,
            defaults=(str(hours),),
        )
    if control_id == "mute.evidence_hours.set":
        try:
            hours = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER)
        if hours < 1 or hours > 168:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER)
        async with session_scope(ctx.bot.sessions) as session:
            row = await session.get(ModSettings, ctx.guild_id)
            before = 24 if row is None else int(row.evidence_remind_hours or 24)
            if row is None:
                session.add(ModSettings(guild_id=ctx.guild_id, evidence_remind_hours=hours))
            else:
                row.evidence_remind_hours = hours
        return AdminResult(
            ok=True,
            field=copy.ADMIN_FIELD_EVIDENCE_HOURS,
            before=str(before),
            after=str(hours),
        )
    if control_id in {"mute.evidence_category", "mute.evidence_archive"}:
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        column = "evidence_category_id" if control_id == "mute.evidence_category" else "evidence_archive_channel_id"
        field = (
            copy.ADMIN_FIELD_EVIDENCE_CATEGORY
            if control_id == "mute.evidence_category"
            else copy.ADMIN_FIELD_EVIDENCE_ARCHIVE
        )
        async with session_scope(ctx.bot.sessions) as session:
            row = await session.get(ModSettings, ctx.guild_id)
            before = None if row is None else getattr(row, column)
            if row is None:
                session.add(ModSettings(guild_id=ctx.guild_id, **{column: channel_id}))
            else:
                setattr(row, column, channel_id)
        return AdminResult(
            ok=True,
            field=field,
            before=_mention_channel(before),
            after=_mention_channel(channel_id),
        )
    try:
        role_id = int(value)
    except (TypeError, ValueError):
        return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
    rejected = owned_pick_error(ctx, role_id, RoleSubsystem.MODERATION)
    if rejected:
        return AdminResult(ok=False, error=rejected)
    async with session_scope(ctx.bot.sessions) as session:
        row = await session.get(ModSettings, ctx.guild_id)
        before = None if row is None else row.mute_role_id
    await ctx.bot.moderation.set_mute_role(ctx.guild_id, role_id)
    return AdminResult(ok=True, field=copy.ADMIN_FIELD_MUTE, before=_mention_role(before), after=_mention_role(role_id))


_OLD_KEYS = {
    "old.ruby": LoyaltyTier.RUBY,
    "old.emerald": LoyaltyTier.EMERALD,
    "old.sapphire": LoyaltyTier.SAPPHIRE,
    "old.amethyst": LoyaltyTier.AMETHYST,
}


async def _ready_old(ctx: AdminCtx) -> ReadyReport:
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        bound = tuple(role_id_for(row, tier) for tier in LoyaltyTier)
    if not any(bound):
        return pack_report((), off=True)
    issues = [
        role_issue(ctx.guild, role_id, fix_control=key, skeleton_key=f"role_{key.removeprefix('old.')}")
        for (key, tier), role_id in zip(_OLD_KEYS.items(), bound, strict=True)
    ]
    found = tuple(item for item in issues if item is not None)
    if found and any(bound):
        return pack_report(found, state=ReadyState.PARTIAL)
    return pack_report(found)


async def _snap_old(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        fields = {TIER_LABELS[tier]: _mention_role(role_id_for(row, tier)) for tier in LoyaltyTier}
    controls = tuple(PageControl("role", key, TIER_LABELS[tier]) for key, tier in _OLD_KEYS.items())
    return PageSnapshot(fields=fields, controls=controls)


async def _handle_old(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    tier = _OLD_KEYS.get(control_id)
    if tier is None:
        return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
    try:
        role_id = int(value)
    except (TypeError, ValueError):
        return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
    async with session_scope(ctx.bot.sessions) as session:
        row = await _config(session, ctx.guild_id)
        before = role_id_for(row, tier)
        set_role_id(row, tier, role_id)
    await ctx.bot.refresh_config(ctx.guild_id)
    protect = getattr(ctx.bot.loyalty, "protect_guild", None)
    if callable(protect) and ctx.guild is not None:
        ctx.bot.spawn(protect(ctx.guild), name=f"loyalty-protect-{ctx.guild_id}")
    return AdminResult(
        ok=True,
        field=TIER_LABELS[tier],
        before=_mention_role(before),
        after=_mention_role(role_id),
    )


def _register_builtin() -> None:
    if _PAGES:
        return
    register(SKELETON_PAGE)
    register(
        AdminPage(
            PAGE_GENERAL,
            copy.ADMIN_PAGE_GENERAL,
            CONFIG_READ,
            _ready_general,
            _snap_general,
            _handle_general,
            keys={"general": CONFIG_MANAGE},
        )
    )
    register(
        AdminPage(
            PAGE_LOGS,
            copy.ADMIN_PAGE_LOGS,
            CONFIG_READ,
            _ready_logs,
            _snap_logs,
            _handle_logs,
            keys={"logs": CONFIG_MANAGE},
        )
    )
    register(
        AdminPage(
            PAGE_RANKS,
            copy.ADMIN_PAGE_RANKS,
            CONFIG_READ,
            _ready_ranks,
            _snap_ranks,
            _handle_ranks,
            keys={"ranks": CONFIG_MANAGE},
        )
    )
    register(
        AdminPage(
            PAGE_PERMS,
            copy.ADMIN_PAGE_PERMS,
            CONFIG_READ,
            _ready_perms,
            _snap_perms,
            _handle_perms,
            keys={
                "perms.prev": CONFIG_READ,
                "perms.next": CONFIG_READ,
                "perms.edit": CONFIG_MANAGE,
                "perms.set": CONFIG_MANAGE,
                "perms.import": CONFIG_MANAGE,
                "perms.export": CONFIG_READ,
                "perms.restore": CONFIG_MANAGE,
                "perms.extra": CONFIG_MANAGE,
            },
        )
    )
    register(
        AdminPage(
            PAGE_STAFFING,
            copy.ADMIN_PAGE_STAFFING,
            CONFIG_READ,
            _ready_staffing,
            _snap_staffing,
            _handle_staffing,
            keys={"staffing": CONFIG_MANAGE},
        )
    )
    register(
        AdminPage(
            PAGE_MUTE,
            copy.ADMIN_PAGE_MUTE,
            CONFIG_READ,
            _ready_mute,
            _snap_mute,
            _handle_mute,
            keys={"mute": MOD_SETTINGS},
        )
    )
    register(QUOTAS_PAGE)
    register(
        AdminPage(
            PAGE_OLD,
            copy.ADMIN_PAGE_OLD,
            CONFIG_READ,
            _ready_old,
            _snap_old,
            _handle_old,
            keys={"old": LOYALTY_MANAGE},
        )
    )
    register(REPUTATION_PAGE)
    register(LADDER_PAGE)
    register(ACHIEVEMENTS_PAGE)
    register(GIVEAWAY_PAGE)
    register(VOICE_PAGE)
    register(TICKETS_PAGE)
    register(AUTOMOD_PAGE)
    register(VERIFICATION_PAGE)
    register(SELFROLES_PAGE)
    register(SHOWCASE_PAGE)
    register(STATS_PAGE)
    register(POLLS_PAGE)
    register(YOUTUBE_PAGE)
    register(LFG_PAGE)
    register(GROUPS_PAGE)
    register(RECRUIT_PAGE)
    register(ALERTS_PAGE)
    register(DATA_PAGE)


_register_builtin()
