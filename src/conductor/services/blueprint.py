"""Выгрузка и применение файла настройки сервера."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import discord
from sqlalchemy import delete, select

from conductor.db.models import (
    GiveawaySettings,
    GuildConfig,
    GuildLayout,
    LadderSettings,
    LfgSettings,
    ModSettings,
    PollSettings,
    PulseAlertSettings,
    ReputationSettings,
    SelfRoleMenu,
    StatsBoard,
    TicketPanel,
    VerificationSettings,
    VoiceHub,
)
from conductor.db.session import session_scope
from conductor.domain.access import (
    LockoutBlocked,
    Rank,
    StaffingPolicy,
    parse_rank_list,
    ranks_for_storage,
)
from conductor.domain.admin import parse_timezone
from conductor.domain.blueprint import (
    BLUEPRINT,
    BLUEPRINT_VERSION,
    HELP_EXPORTED_KEY,
    HELP_GUILD_KEY,
    HELP_KEY,
    HELP_RULES_KEY,
    HELP_VERSION_KEY,
    HELP_WHAT_KEY,
    NAME_KEY,
    ROOT_ROLES,
    ROOT_SHOWCASES,
    BlueprintIssue,
    BlueprintPlan,
    BlueprintReport,
    FieldKind,
    FieldSpec,
    IssueLevel,
    parse_blueprint,
    plan_blueprint,
    put_path,
    render_docs,
    spec_by_path,
)
from conductor.domain.mapping import AUTO_BIND_THRESHOLD, rank_candidates
from conductor.domain.rooms import dump_hub_settings, parse_hub_settings
from conductor.domain.seed import SeedMode, SkeletonKind, entry_by_key
from conductor.domain.self_roles import sanitize_label
from conductor.domain.tickets import dump_ticket_settings, parse_ticket_settings
from conductor.presentation import copy
from conductor.services.access import (
    RankRoleTaken,
    cached_snapshot,
    set_key_extra_roles,
    set_key_ranks,
    set_rank_role,
)
from conductor.services.admin_pages import log_change
from conductor.services.guild_config import get_or_create_config
from conductor.services.watchers import load_policy, save_policy

__all__ = ["BlueprintService"]


class BlueprintService:
    """Экспорт, проверка и применение JSON-файла настройки."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def export(self, guild: discord.Guild, *, with_docs: bool = True, only: tuple[str, ...] = ()) -> bytes:
        payload = await self.snapshot_payload(guild)
        if only:
            payload = {key: value for key, value in payload.items() if key in only}
        if with_docs:
            payload = render_docs(payload)
            payload[HELP_KEY] = {
                HELP_WHAT_KEY: copy.BLUEPRINT_WHAT,
                HELP_RULES_KEY: list(copy.BLUEPRINT_RULES),
                HELP_VERSION_KEY: BLUEPRINT_VERSION,
                HELP_EXPORTED_KEY: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                HELP_GUILD_KEY: {"id": int(guild.id), NAME_KEY: str(getattr(guild, "name", "") or "")},
            }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return text.encode("utf-8")

    async def preview(self, guild: discord.Guild, raw: bytes) -> BlueprintPlan:
        issues: list[BlueprintIssue] = []
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return BlueprintPlan((), (BlueprintIssue(IssueLevel.ERROR, "", "bad_json"),), (), ())
        if not isinstance(decoded, dict):
            return BlueprintPlan((), (BlueprintIssue(IssueLevel.ERROR, "", "bad_type"),), (), ())
        wanted, parse_issues = parse_blueprint(decoded)
        issues.extend(parse_issues)
        current = await self.snapshot_payload(guild)
        resolved, locate_issues = _resolve(guild, wanted)
        issues.extend(locate_issues)
        snapshot = await cached_snapshot(self._bot, int(guild.id))
        plan = plan_blueprint(
            current,
            wanted,
            resolved=resolved,
            rank_roles=snapshot.rank_roles,
            actor=Rank.SPECIAL,
        )
        merged = (*issues, *plan.issues)
        return BlueprintPlan(plan.changes, merged, plan.unknown, plan.creates)

    async def apply(
        self,
        guild: discord.Guild,
        plan: BlueprintPlan,
        *,
        actor_id: int,
        build: bool = True,
        wanted: Mapping[str, Any] | None = None,
    ) -> BlueprintReport:
        if plan.blocked:
            return BlueprintReport(failed=tuple(item.path for item in plan.issues if item.level is IssueLevel.ERROR))
        body = dict(wanted or {})
        applied: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        notes: list[str] = []
        await self._apply_skeleton(
            guild,
            plan,
            body,
            actor_id=actor_id,
            build=build,
            applied=applied,
            failed=failed,
            notes=notes,
        )
        await self._apply_rights(guild, plan, body, actor_id=actor_id, applied=applied, failed=failed)
        await self._apply_settings(guild, plan, body, actor_id=actor_id, applied=applied, failed=failed)
        await self._apply_showcases(guild, plan, actor_id=actor_id, applied=applied, failed=failed, notes=notes)
        for change in plan.changes:
            if change.path not in applied and change.path not in failed:
                skipped.append(change.path)
        return BlueprintReport(tuple(applied), tuple(skipped), tuple(failed), tuple(notes))

    async def snapshot_payload(self, guild: discord.Guild) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        existing = {}
        seed = getattr(self._bot, "seed", None)
        snap = getattr(seed, "snapshot", None)
        if callable(snap):
            existing = await snap(guild, with_old=True)
        access = await cached_snapshot(self._bot, int(guild.id))
        async with session_scope(self._bot.sessions) as session:
            config = await get_or_create_config(session, int(guild.id))
            rows = {
                "guild_configs": config,
                "verification_settings": await session.get(VerificationSettings, int(guild.id)),
                "mod_settings": await session.get(ModSettings, int(guild.id)),
                "poll_settings": await session.get(PollSettings, int(guild.id)),
                "lfg_settings": await session.get(LfgSettings, int(guild.id)),
                "stats_boards": await session.get(StatsBoard, int(guild.id)),
                "pulse_alert_settings": await session.get(PulseAlertSettings, int(guild.id)),
                "voice_hubs": await session.get(VoiceHub, int(guild.id)),
                "ladder_settings": await session.get(LadderSettings, int(guild.id)),
                "reputation_config": await session.get(ReputationSettings, int(guild.id)),
                "giveaway_settings": await session.get(GiveawaySettings, int(guild.id)),
            }
            policy = await load_policy(session, int(guild.id))
            tickets = (
                await session.scalars(select(TicketPanel).where(TicketPanel.guild_id == int(guild.id)))
            ).first()
            menus = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.guild_id == int(guild.id)))).all()
            layout_rows = await session.scalars(select(GuildLayout).where(GuildLayout.guild_id == int(guild.id)))
            layout = {row.key: int(row.channel_id) for row in layout_rows.all()}
            for spec in BLUEPRINT:
                put_path(payload, spec.path, _read_spec(spec, existing, access, rows, policy, tickets, menus, layout))
        return payload

    async def _apply_skeleton(
        self,
        guild: discord.Guild,
        plan: BlueprintPlan,
        wanted: Mapping[str, Any],
        *,
        actor_id: int,
        build: bool,
        applied: list[str],
        failed: list[str],
        notes: list[str],
    ) -> None:
        seed = getattr(self._bot, "seed", None)
        ids: dict[str, int] = {}
        clears: list[FieldSpec] = []
        for change in plan.changes:
            spec = spec_by_path(change.path)
            if spec is None or not spec.skeleton_key:
                continue
            value = _nested(wanted, change.path)
            if value is None:
                clears.append(spec)
                continue
            object_id = _binding_id(value)
            if object_id is not None:
                ids[spec.skeleton_key] = object_id
        if ids and callable(getattr(seed, "bind_existing", None)):
            await seed.bind_existing(guild, ids, actor_id=actor_id)
            applied.extend(
                change.path
                for change in plan.changes
                if (spec := spec_by_path(change.path)) is not None and spec.skeleton_key in ids
            )
            for path in list(applied[-len(ids) :]):
                _log(self._bot, guild.id, path, actor_id)
        if clears:
            async with session_scope(self._bot.sessions) as session:
                for spec in clears:
                    await _clear_binding(session, int(guild.id), spec)
                    applied.append(spec.path)
                    _log(self._bot, guild.id, spec.path, actor_id)
        if build and plan.creates and callable(getattr(seed, "run", None)):
            report = await seed.run(
                guild,
                actor_id=actor_id,
                mode=SeedMode.BUILD,
                only_keys=frozenset(plan.creates),
            )
            notes.extend(report.notes)
            for key in plan.creates:
                spec = next((item for item in BLUEPRINT if item.skeleton_key == key), None)
                if spec is not None:
                    applied.append(spec.path)

    async def _apply_rights(
        self,
        guild: discord.Guild,
        plan: BlueprintPlan,
        wanted: Mapping[str, Any],
        *,
        actor_id: int,
        applied: list[str],
        failed: list[str],
    ) -> None:
        async with session_scope(self._bot.sessions) as session:
            for change in plan.changes:
                spec = spec_by_path(change.path)
                if spec is None or spec.table not in {"guild_ranks", "guild_permissions"}:
                    continue
                value = _nested(wanted, change.path)
                try:
                    if spec.table == "guild_ranks":
                        rank = Rank[spec.column.upper()]
                        if value is None:
                            continue
                        role_id = _first_id(value)
                        if role_id is None:
                            continue
                        await set_rank_role(session, int(guild.id), rank, role_id)
                    elif spec.column.endswith(".extra"):
                        key = spec.column.removesuffix(".extra")
                        roles = () if value is None else tuple(int(item) for item in value)
                        await set_key_extra_roles(session, int(guild.id), key, roles)
                    else:
                        ranks = parse_rank_list(value if isinstance(value, (list, tuple)) else str(value))
                        await set_key_ranks(
                            session,
                            int(guild.id),
                            spec.column,
                            ranks,
                            actor=Rank.SPECIAL,
                        )
                except (TypeError, ValueError, AttributeError, KeyError, LockoutBlocked, RankRoleTaken):
                    failed.append(change.path)
                    continue
                applied.append(change.path)
                _log(self._bot, guild.id, change.path, actor_id)
        invalidate = getattr(getattr(self._bot, "caches", None), "invalidate_access", None)
        if callable(invalidate):
            invalidate(int(guild.id))

    async def _apply_settings(
        self,
        guild: discord.Guild,
        plan: BlueprintPlan,
        wanted: Mapping[str, Any],
        *,
        actor_id: int,
        applied: list[str],
        failed: list[str],
    ) -> None:
        async with session_scope(self._bot.sessions) as session:
            config = await get_or_create_config(session, int(guild.id))
            for change in plan.changes:
                spec = spec_by_path(change.path)
                skipped = spec.table in {"guild_ranks", "guild_permissions", "panels", ""}
                if spec is None or spec.skeleton_key or skipped:
                    continue
                value = _nested(wanted, change.path)
                try:
                    await _write_spec(session, int(guild.id), spec, value, config)
                except (TypeError, ValueError, AttributeError, KeyError):
                    failed.append(change.path)
                    continue
                applied.append(change.path)
                _log(self._bot, guild.id, change.path, actor_id)
            await session.flush()
        refresh = getattr(self._bot, "refresh_config", None)
        if callable(refresh):
            await refresh(int(guild.id))

    async def _apply_showcases(
        self,
        guild: discord.Guild,
        plan: BlueprintPlan,
        *,
        actor_id: int,
        applied: list[str],
        failed: list[str],
        notes: list[str],
    ) -> None:
        prefix = f"{ROOT_SHOWCASES}."
        if not any(change.path.startswith(prefix) for change in plan.changes):
            return
        poster = getattr(getattr(self._bot, "showcase", None), "post_missing", None)
        if not callable(poster):
            return
        posted = await poster(guild, actor_id=actor_id)
        if not posted:
            return
        failed_item = next((item for item in posted if not item.ok), None)
        if failed_item is not None:
            failed.append(ROOT_SHOWCASES)
            notes.append(failed_item.error or copy.STALE_BODY)
            return
        applied.append(ROOT_SHOWCASES)
        _log(self._bot, guild.id, ROOT_SHOWCASES, actor_id)


def _read_spec(
    spec: FieldSpec,
    existing: Mapping[str, Any],
    access: Any,
    rows: Mapping[str, Any],
    policy: Any,
    tickets: Any,
    menus: Any,
    layout: Mapping[str, int],
) -> Any:
    if spec.kind is FieldKind.BINDING:
        item = existing.get(spec.skeleton_key)
        bound = None if item is None else getattr(item, "bound_id", None)
        name = ""
        entry = entry_by_key(spec.skeleton_key)
        if entry is not None:
            name = entry.name
        if bound and getattr(item, "exists", False):
            return {"id": int(bound), NAME_KEY: name}
        return {"id": None, NAME_KEY: name}
    if spec.table == "guild_ranks":
        roles = tuple(int(item) for item in access.rank_roles.get(Rank[spec.column.upper()], ()))
        return list(roles)
    if spec.table == "guild_permissions" and spec.column.endswith(".extra"):
        key = spec.column.removesuffix(".extra")
        return list(int(item) for item in access.rule(key).extra_role_ids)
    if spec.table == "guild_permissions":
        return list(ranks_for_storage(access.rule(spec.column).ranks))
    if spec.table == "staffing_policy":
        return bool(getattr(policy, spec.column, False))
    if spec.table == "guild_layout":
        found = layout.get(spec.column)
        return None if found is None else int(found)
    if spec.table == "ticket_panels":
        if tickets is None:
            return None
        if spec.column == "channel_id":
            return int(tickets.channel_id)
        settings = parse_ticket_settings(tickets.settings)
        attr = spec.column.removeprefix("settings.")
        return getattr(settings, attr, None)
    if spec.table == "voice_hubs":
        row = rows.get("voice_hubs")
        if row is None:
            return None
        if spec.column == "channel_id":
            return int(row.channel_id)
        settings = parse_hub_settings(row.settings)
        attr = spec.column.removeprefix("settings.")
        return getattr(settings, attr, None)
    if spec.table == "self_role_menus":
        menu = next(iter(menus), None)
        if menu is None:
            return None
        return getattr(menu, spec.column, None)
    row = rows.get(spec.table)
    if row is None:
        return None
    if "." in spec.column:
        return None
    value = getattr(row, spec.column, None)
    if spec.kind is FieldKind.BOOL:
        return bool(value)
    return value


async def _write_spec(session: Any, guild_id: int, spec: FieldSpec, value: Any, config: GuildConfig) -> None:
    if spec.table == "guild_configs":
        _set_attr(config, spec.column, _scalar(spec, value))
        if spec.column == "timezone":
            parsed = parse_timezone(str(value or ""))
            if parsed:
                config.timezone = parsed
        return
    if spec.table == "staffing_policy":
        current = await load_policy(session, guild_id)
        kwargs = {"senior_appoint": current.senior_appoint, "special_appoint": current.special_appoint}
        kwargs[spec.column] = bool(value)
        await save_policy(session, guild_id, StaffingPolicy(**kwargs))
        return
    if spec.table == "ticket_panels":
        row = (await session.scalars(select(TicketPanel).where(TicketPanel.guild_id == guild_id))).first()
        if row is None:
            return
        if spec.column == "channel_id":
            row.channel_id = int(value)
            return
        settings = parse_ticket_settings(row.settings)
        attr = spec.column.removeprefix("settings.")
        settings = replace(settings, **{attr: _scalar(spec, value)})
        row.settings = dump_ticket_settings(settings)
        return
    if spec.table == "voice_hubs":
        row = await session.get(VoiceHub, guild_id)
        if row is None and spec.column == "channel_id" and value is not None:
            row = VoiceHub(guild_id=guild_id, channel_id=int(value), settings="{}")
            session.add(row)
            return
        if row is None:
            return
        if spec.column == "channel_id":
            if value is None:
                await session.delete(row)
                return
            row.channel_id = int(value)
            return
        settings = parse_hub_settings(row.settings)
        attr = spec.column.removeprefix("settings.")
        mapped = _scalar(spec, value) if attr != "template" else sanitize_label(str(value or "")) or ""
        settings = replace(settings, **{attr: mapped})
        row.settings = dump_hub_settings(settings)
        return
    model = _MODEL.get(spec.table)
    if model is None:
        return
    row = await session.get(model, guild_id)
    if row is None:
        row = model(guild_id=guild_id)
        session.add(row)
    _set_attr(row, spec.column, _scalar(spec, value))


async def _clear_binding(session: Any, guild_id: int, spec: FieldSpec) -> None:
    if spec.table == "guild_layout":
        await session.execute(
            delete(GuildLayout).where(GuildLayout.guild_id == guild_id, GuildLayout.key == spec.column)
        )
        return
    if spec.table == "guild_configs":
        config = await get_or_create_config(session, guild_id)
        _set_attr(config, spec.column, None)
        return
    model = _MODEL.get(spec.table)
    if model is None:
        return
    row = await session.get(model, guild_id)
    if row is not None:
        _set_attr(row, spec.column.split(".", 1)[0] if "." not in spec.column else spec.column, None)


def _resolve(
    guild: discord.Guild, wanted: Mapping[str, Any]
) -> tuple[dict[str, int | None], tuple[BlueprintIssue, ...]]:
    resolved: dict[str, int | None] = {}
    issues: list[BlueprintIssue] = []
    channels = tuple(
        (int(channel.id), str(getattr(channel, "name", "")), getattr(channel, "category_id", None))
        for channel in getattr(guild, "channels", ())
        if getattr(channel, "id", None)
    )
    roles = tuple(
        (int(role.id), str(getattr(role, "name", "")), None)
        for role in getattr(guild, "roles", ())
        if getattr(role, "id", None)
    )
    for spec in BLUEPRINT:
        value = _nested(wanted, spec.path)
        if value is None:
            continue
        object_id = _binding_id(value) if spec.kind is FieldKind.BINDING else _as_id(value)
        name = ""
        if spec.kind is FieldKind.BINDING and isinstance(value, Mapping):
            name = str(value.get(NAME_KEY) or "").strip()
        if object_id is not None:
            status = _locate(guild, spec, object_id)
            if status == "foreign":
                issues.append(BlueprintIssue(IssueLevel.ERROR, spec.path, "foreign_guild"))
                continue
            if status == "missing":
                issues.append(BlueprintIssue(IssueLevel.WARN, spec.path, "missing_object"))
                resolved[spec.path] = None
                continue
            resolved[spec.path] = object_id
            continue
        if name and spec.skeleton_key:
            entry = entry_by_key(spec.skeleton_key)
            if entry is None:
                continue
            pool = roles if entry.kind is SkeletonKind.ROLE else channels
            ranked = rank_candidates(entry, pool)
            top = ranked[:2]
            if len(top) > 1 and abs(top[0].score - top[1].score) < 0.05:
                issues.append(BlueprintIssue(IssueLevel.WARN, spec.path, "ambiguous_name"))
            elif top and top[0].score >= AUTO_BIND_THRESHOLD:
                resolved[spec.path] = int(top[0].object_id)
    return resolved, tuple(issues)


def _locate(guild: discord.Guild, spec: FieldSpec, object_id: int) -> str:
    role_path = spec.path.startswith(f"{ROOT_ROLES}.")
    getter = guild.get_role if spec.kind is FieldKind.ROLE or role_path else guild.get_channel
    found = getter(int(object_id))
    if found is None and spec.kind is not FieldKind.ROLE and not role_path:
        found = guild.get_role(int(object_id)) if spec.kind is FieldKind.ROLE else None
    if found is None:
        return "missing"
    owner = getattr(found, "guild", None)
    gid = getattr(owner, "id", None) if owner is not None else getattr(guild, "id", None)
    if gid is not None and int(gid) != int(guild.id):
        return "foreign"
    return "ok"


def _nested(payload: Mapping[str, Any], path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _binding_id(value: Any) -> int | None:
    if isinstance(value, Mapping):
        return _as_id(value.get("id"))
    return _as_id(value)


def _as_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _first_id(value: Any) -> int | None:
    if isinstance(value, list):
        return _as_id(value[0] if value else None)
    return _as_id(value)


def _scalar(spec: FieldSpec, value: Any) -> Any:
    if value is None:
        return None
    if spec.kind is FieldKind.BOOL:
        return bool(value)
    if spec.kind is FieldKind.INT:
        return int(value)
    if spec.kind in {FieldKind.CHANNEL, FieldKind.CATEGORY, FieldKind.VOICE, FieldKind.ROLE}:
        return _as_id(value)
    if spec.kind is FieldKind.TEXT:
        return sanitize_label(str(value)) or str(value)
    return value


def _set_attr(row: Any, column: str, value: Any) -> None:
    if hasattr(row, column):
        setattr(row, column, value)


def _log(bot: Any, guild_id: int, path: str, actor_id: int) -> None:
    log_change(bot, guild_id, page=copy.BLUEPRINT_PAGE, field=path, before="—", after="1", actor_id=actor_id)


_MODEL: dict[str, type] = {
    "verification_settings": VerificationSettings,
    "mod_settings": ModSettings,
    "poll_settings": PollSettings,
    "lfg_settings": LfgSettings,
    "stats_boards": StatsBoard,
    "pulse_alert_settings": PulseAlertSettings,
    "ladder_settings": LadderSettings,
    "reputation_config": ReputationSettings,
    "giveaway_settings": GiveawaySettings,
}
