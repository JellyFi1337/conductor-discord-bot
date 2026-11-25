"""Слежение за каркасом: отвязка удалённых объектов и канал #структура."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import discord
import structlog
from sqlalchemy import delete, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    AchievementDef,
    GroupHub,
    GuildConfig,
    GuildLayout,
    GuildRank,
    LadderSettings,
    LfgSettings,
    ModSettings,
    PollSettings,
    ReputationTier,
    SelfRoleMenu,
    SelfRoleOption,
    StatsBoard,
    TicketPanel,
    VerificationSettings,
    VoiceHub,
)
from conductor.db.session import session_scope
from conductor.domain.access import Rank, rank_holding_role
from conductor.domain.observe import cap_cards, fingerprint_changed, structure_fingerprint
from conductor.domain.seed import (
    SKELETON,
    SeedMode,
    SkeletonKind,
    entry_by_key,
    is_same_name,
    skeleton_setting_fields,
)
from conductor.domain.tickets import dump_ticket_settings, parse_ticket_settings
from conductor.presentation.structure import structure_lost_card, structure_more_card, structure_renamed_card
from conductor.services.access import cached_snapshot, unlock_if_orphaned
from conductor.services.guild_config import get_or_create_config
from conductor.services.panels import mark_stale_by_channel, mark_stale_by_ref
from conductor.services.role_broker import RoleClaim, RoleSubsystem
from conductor.services.runtime import log_tick

log = structlog.get_logger("conductor.structure")

__all__ = ["StructureLoss", "StructureWatch", "note_self_deleted"]

_RANK_KEYS: dict[str, Rank] = {
    "role_special": Rank.SPECIAL,
    "role_chief": Rank.CHIEF,
    "role_senior": Rank.SENIOR,
    "role_watcher": Rank.WATCHER,
}
_PK_MODELS: dict[str, type[Any]] = {
    "guild_configs": GuildConfig,
    "mod_settings": ModSettings,
    "verification_settings": VerificationSettings,
    "ladder_settings": LadderSettings,
    "lfg_settings": LfgSettings,
    "poll_settings": PollSettings,
    "stats_boards": StatsBoard,
    "voice_hubs": VoiceHub,
    "group_hubs": GroupHub,
}
_ROW_MODELS: dict[str, type[Any]] = {
    "ticket_panels": TicketPanel,
    "self_role_menus": SelfRoleMenu,
    "self_role_options": SelfRoleOption,
    "reputation_tiers": ReputationTier,
    "achievement_defs": AchievementDef,
}


@dataclass(frozen=True, slots=True)
class StructureLoss:
    key: str
    label: str
    kind: SkeletonKind
    object_id: int
    feature: str
    bindings_cleared: tuple[str, ...]
    panels_stale: int
    rank: Rank | None = None
    holders: tuple[int, ...] = ()
    subsystem: str = ""
    problem: str = "gone"


class StructureWatch:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._self_deleted: set[tuple[int, int]] = set()
        self._holders: dict[tuple[int, str], tuple[int, ...]] = {}
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(3600, "structure-sweep", self.sweep_all)

    def mark_self_deleted(self, guild_id: int, object_id: int) -> None:
        self._self_deleted.add((int(guild_id), int(object_id)))

    def _take_self_deleted(self, guild_id: int, object_id: int) -> bool:
        key = (int(guild_id), int(object_id))
        if key not in self._self_deleted:
            return False
        self._self_deleted.discard(key)
        return True

    async def on_channel_gone(self, guild: discord.Guild, channel_id: int) -> StructureLoss | None:
        if self._take_self_deleted(int(guild.id), int(channel_id)):
            return None
        return await self._forget(guild, object_id=int(channel_id), role=False)

    async def on_role_gone(self, guild: discord.Guild, role_id: int) -> StructureLoss | None:
        if self._take_self_deleted(int(guild.id), int(role_id)):
            return None
        return await self._forget(guild, object_id=int(role_id), role=True)

    async def on_renamed(self, channel: discord.abc.GuildChannel, *, old_name: str) -> StructureLoss | None:
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None
        object_id = int(channel.id)
        new_name = str(getattr(channel, "name", "") or "")
        existing = await self._bot.seed.snapshot(guild, with_old=True)
        matched = tuple(
            key for key, item in existing.items() if item.bound_id and int(item.bound_id) == object_id
        )
        if not matched:
            async with session_scope(self._bot.sessions) as session:
                matched = await _keys_for_id(session, int(guild.id), object_id)
        if not matched:
            return None
        key = matched[0]
        entry = entry_by_key(key)
        if entry is None:
            return None
        names = (entry.name, *entry.aliases)
        if any(is_same_name(new_name, name) for name in names):
            return None
        loss = StructureLoss(
            key=entry.key,
            label=entry.diagnose_label,
            kind=entry.kind,
            object_id=object_id,
            feature=entry.feature,
            bindings_cleared=(),
            panels_stale=0,
            problem="renamed",
        )
        await self._emit(guild, (loss,), renamed=(old_name, new_name))
        return loss

    async def notify(self, guild: discord.Guild, loss: StructureLoss) -> None:
        await self._emit(guild, (loss,))

    async def notify_many(self, guild: discord.Guild, losses: tuple[StructureLoss, ...]) -> None:
        await self._emit(guild, losses)

    async def sweep(self, guild: discord.Guild) -> tuple[StructureLoss, ...]:
        existing = await self._bot.seed.snapshot(guild, with_old=True)
        found: list[StructureLoss] = []
        for key, item in existing.items():
            if not item.bound_id or item.exists:
                continue
            entry = entry_by_key(key)
            loss = await self._forget(
                guild,
                object_id=int(item.bound_id),
                role=entry is not None and entry.kind is SkeletonKind.ROLE,
                only_keys=frozenset({key}),
            )
            if loss is not None:
                found.append(loss)
        if found:
            await self._emit(guild, tuple(found))
        return tuple(found)

    async def sweep_all(self) -> None:
        changed = 0
        for guild in tuple(getattr(self._bot, "guilds", ())):
            serves = getattr(self._bot, "serves", None)
            if callable(serves) and not serves(int(guild.id)):
                continue
            try:
                found = await self.sweep(guild)
            except Exception:
                log.exception("structure_sweep_failed", guild_id=int(getattr(guild, "id", 0) or 0))
                continue
            changed += len(found)
        log_tick(log, "structure_sweep", changed=changed)

    async def restore(self, guild: discord.Guild, key: str, *, actor_id: int) -> Any:
        holders = self._holders.pop((int(guild.id), key), ())
        report = await self._bot.seed.repair(guild, key, actor_id=actor_id, mode=SeedMode.BUILD)
        if not holders:
            return report
        existing = await self._bot.seed.snapshot(guild, with_old=True)
        item = existing.get(key)
        role_id = int(item.bound_id or 0) if item is not None else 0
        if not role_id:
            return report
        apply = getattr(getattr(self._bot, "roles", None), "apply", None)
        if not callable(apply):
            return report
        for user_id in holders:
            try:
                await apply(
                    RoleClaim(
                        guild_id=int(guild.id),
                        user_id=int(user_id),
                        subsystem=RoleSubsystem.STAFF,
                        add=(role_id,),
                        reason="structure restore",
                        urgent=True,
                        actor_id=actor_id,
                    )
                )
            except Exception:
                log.exception("structure_restore_role_failed", guild_id=int(guild.id), user_id=int(user_id))
        return report

    async def _forget(
        self,
        guild: discord.Guild,
        *,
        object_id: int,
        role: bool,
        only_keys: frozenset[str] | None = None,
    ) -> StructureLoss | None:
        existing = await self._bot.seed.snapshot(guild, with_old=True)
        keys = tuple(
            key for key, item in existing.items() if item.bound_id and int(item.bound_id) == int(object_id)
        )
        async with session_scope(self._bot.sessions) as session:
            extra = await _keys_for_id(session, int(guild.id), int(object_id))
        keys = tuple(dict.fromkeys((*keys, *extra)))
        if only_keys is not None:
            keys = tuple(key for key in keys if key in only_keys)
        rank: Rank | None = None
        holders: tuple[int, ...] = ()
        if role and keys:
            snapshot = await cached_snapshot(self._bot, int(guild.id))
            rank = rank_holding_role(snapshot.rank_roles, object_id)
            holders = _role_holders(guild, object_id)
            for key in keys:
                mapped = _RANK_KEYS.get(key)
                if mapped is not None:
                    rank = mapped
                    break
        entry = next((found for key in keys if (found := entry_by_key(key)) is not None), None)
        kind = entry.kind if entry is not None else (SkeletonKind.ROLE if role else SkeletonKind.TEXT)
        subsystem = ""
        owner = getattr(getattr(self._bot, "roles", None), "owner_of", None)
        if role and callable(owner):
            found = owner(int(guild.id), int(object_id))
            subsystem = "" if found is None else str(found.value)
        keep_rank = rank is not None and rank is not Rank.SPECIAL
        cleared, panels = await self._clear(
            guild,
            object_id=int(object_id),
            keys=keys,
            keep_rank=keep_rank,
            unlock_special=rank is Rank.SPECIAL,
        )
        if not keys and not panels:
            return None
        invalidate = getattr(getattr(self._bot, "caches", None), "invalidate_access", None)
        if callable(invalidate):
            invalidate(int(guild.id))
        refresh = getattr(self._bot, "refresh_config", None)
        if callable(refresh):
            await refresh(int(guild.id))
        loss = StructureLoss(
            key="" if entry is None else entry.key,
            label="" if entry is None else entry.diagnose_label,
            kind=kind,
            object_id=int(object_id),
            feature="" if entry is None else entry.feature,
            bindings_cleared=cleared,
            panels_stale=panels,
            rank=rank,
            holders=holders,
            subsystem=subsystem,
            problem="gone" if keys else "panel",
        )
        if holders and loss.key:
            self._holders[(int(guild.id), loss.key)] = holders
        return loss

    async def _clear(
        self,
        guild: discord.Guild,
        *,
        object_id: int,
        keys: tuple[str, ...],
        keep_rank: bool,
        unlock_special: bool,
    ) -> tuple[tuple[str, ...], int]:
        cleared: list[str] = []
        panels = 0
        wanted = set(keys)
        async with session_scope(self._bot.sessions) as session:
            for key, path in _binding_paths():
                if key:
                    if key not in wanted:
                        continue
                elif not await _path_holds(session, int(guild.id), path, object_id, key=key):
                    continue
                if await _clear_path(session, int(guild.id), path, object_id, keep_rank=keep_rank):
                    cleared.append(path)
            for key in keys:
                panels += await mark_stale_by_ref(session, guild_id=int(guild.id), kind="sk", ref=key)
            panels += await mark_stale_by_channel(session, guild_id=int(guild.id), channel_id=int(object_id))
            if unlock_special:
                known = tuple(int(role.id) for role in getattr(guild, "roles", ()) if getattr(role, "id", None))
                await unlock_if_orphaned(session, int(guild.id), known_role_ids=known)
        return tuple(dict.fromkeys(cleared)), panels

    async def _emit(
        self,
        guild: discord.Guild,
        losses: tuple[StructureLoss, ...],
        *,
        renamed: tuple[str, str] | None = None,
    ) -> None:
        if not losses:
            return
        items = tuple((loss.key or str(loss.object_id), loss.problem) for loss in losses)
        current = structure_fingerprint(items)
        previous = await self._load_fingerprint(int(guild.id))
        if not fingerprint_changed(previous, current):
            return
        shown, rest = cap_cards(losses)
        channel_id = await self._target_channel(int(guild.id))
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit) or channel_id is None:
            log.info(
                "structure_notify_log_only",
                guild_id=int(guild.id),
                keys=tuple(loss.key for loss in losses),
            )
            await self._store_fingerprint(int(guild.id), current)
            return
        for loss in shown:
            if renamed is not None and loss.problem == "renamed":
                view = structure_renamed_card(label=loss.label, old=renamed[0], new=renamed[1])
            else:
                view = structure_lost_card(loss)
            submit(channel_id, view=view)
        if rest:
            submit(channel_id, view=structure_more_card(rest))
        await self._store_fingerprint(int(guild.id), current)

    async def _target_channel(self, guild_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            config = await session.get(GuildConfig, guild_id)
            if config is None:
                return None
            return int(config.structure_channel_id or config.admin_log_channel_id or 0) or None

    async def _load_fingerprint(self, guild_id: int) -> str | None:
        async with session_scope(self._bot.sessions) as session:
            config = await session.get(GuildConfig, guild_id)
            if config is None:
                return None
            value = getattr(config, "structure_fingerprint", None)
            return None if not value else str(value)

    async def _store_fingerprint(self, guild_id: int, value: str) -> None:
        async with session_scope(self._bot.sessions) as session:
            config = await get_or_create_config(session, guild_id)
            config.structure_fingerprint = value


def note_self_deleted(bot: Any, guild_id: int, object_id: int) -> None:
    mark = getattr(getattr(bot, "structure", None), "mark_self_deleted", None)
    if callable(mark):
        mark(int(guild_id), int(object_id))


def _binding_paths() -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in SKELETON:
        for path in (entry.binding, *entry.also_bind):
            if not path:
                continue
            rows.append((entry.key, path))
            seen.add(path)
    for path in skeleton_setting_fields():
        if path not in seen:
            rows.append(("", path))
    return tuple(rows)


async def _keys_for_id(session: AsyncSession, guild_id: int, object_id: int) -> tuple[str, ...]:
    found: list[str] = []
    for key, path in _binding_paths():
        if not key:
            continue
        if await _path_holds(session, guild_id, path, object_id, key=key):
            found.append(key)
    return tuple(dict.fromkeys(found))


_SHARED_PATHS = frozenset({"guild_ranks.role_id", "reputation_tiers.role_id", "achievement_defs.role_id"})


async def _path_holds(session: AsyncSession, guild_id: int, path: str, object_id: int, *, key: str = "") -> bool:
    if path in _SHARED_PATHS and path == "guild_ranks.role_id":
        rank = _RANK_KEYS.get(key)
        if rank is None:
            return False
        row = await session.scalar(
            select(GuildRank).where(
                GuildRank.guild_id == guild_id,
                GuildRank.role_id == int(object_id),
                GuildRank.rank == rank.key,
            )
        )
        return row is not None
    if path in _SHARED_PATHS:
        return False
    if path.startswith("guild_layout:"):
        layout_key = path.split(":", 1)[1]
        row = await session.scalar(
            select(GuildLayout).where(GuildLayout.guild_id == guild_id, GuildLayout.key == layout_key)
        )
        return row is not None and int(row.channel_id) == int(object_id)
    if path.startswith("ticket_panels.settings."):
        field = path.rsplit(".", 1)[-1]
        rows = (await session.scalars(select(TicketPanel).where(TicketPanel.guild_id == guild_id))).all()
        for row in rows:
            settings = parse_ticket_settings(row.settings)
            current = getattr(settings, field, None)
            if int(current or 0) == int(object_id):
                return True
        return False
    if "." not in path:
        return False
    table, column = path.split(".", 1)
    if table == "guild_layout":
        row = await session.scalar(
            select(GuildLayout).where(GuildLayout.guild_id == guild_id, GuildLayout.channel_id == int(object_id))
        )
        return row is not None
    model = _PK_MODELS.get(table)
    if model is not None:
        row = await session.get(model, guild_id)
        if row is None or not hasattr(row, column):
            return False
        current = getattr(row, column)
        return current is not None and int(current) == int(object_id)
    model = _ROW_MODELS.get(table)
    if model is None or not hasattr(model, column):
        return False
    row = await session.scalar(
        select(model).where(model.guild_id == guild_id, getattr(model, column) == int(object_id))
    )
    return row is not None


async def _clear_path(
    session: AsyncSession,
    guild_id: int,
    path: str,
    object_id: int,
    *,
    keep_rank: bool,
) -> bool:
    if path.startswith("guild_layout:"):
        key = path.split(":", 1)[1]
        result = await session.execute(
            delete(GuildLayout).where(GuildLayout.guild_id == guild_id, GuildLayout.key == key)
        )
        return int(result.rowcount or 0) > 0
    if path.startswith("ticket_panels.settings."):
        field = path.rsplit(".", 1)[-1]
        changed = False
        rows = (await session.scalars(select(TicketPanel).where(TicketPanel.guild_id == guild_id))).all()
        for row in rows:
            settings = parse_ticket_settings(row.settings)
            current = getattr(settings, field, None)
            if int(current or 0) != int(object_id):
                continue
            row.settings = dump_ticket_settings(replace(settings, **{field: None}))
            changed = True
        return changed
    if "." not in path:
        return False
    table, column = path.split(".", 1)
    if table == "guild_ranks":
        if keep_rank:
            return False
        result = await session.execute(
            delete(GuildRank).where(GuildRank.guild_id == guild_id, GuildRank.role_id == int(object_id))
        )
        return int(result.rowcount or 0) > 0
    if table == "guild_layout":
        result = await session.execute(
            delete(GuildLayout).where(GuildLayout.guild_id == guild_id, GuildLayout.channel_id == int(object_id))
        )
        return int(result.rowcount or 0) > 0
    model = _PK_MODELS.get(table)
    if model is not None:
        row = await session.get(model, guild_id)
        if row is None or not hasattr(row, column):
            return False
        current = getattr(row, column)
        if current is None or int(current) != int(object_id):
            return False
        setattr(row, column, None if _column_nullable(model, column) else 0)
        return True
    model = _ROW_MODELS.get(table)
    if model is None or not hasattr(model, column):
        log.warning("structure_unknown_binding", path=path, guild_id=guild_id)
        return False
    if _column_nullable(model, column):
        result = await session.execute(
            update(model)
            .where(model.guild_id == guild_id, getattr(model, column) == int(object_id))
            .values({column: None})
        )
        return int(result.rowcount or 0) > 0
    if table == "self_role_options":
        result = await session.execute(
            delete(SelfRoleOption).where(
                SelfRoleOption.guild_id == guild_id, SelfRoleOption.role_id == int(object_id)
            )
        )
        return int(result.rowcount or 0) > 0
    result = await session.execute(
        update(model)
        .where(model.guild_id == guild_id, getattr(model, column) == int(object_id))
        .values({column: 0})
    )
    return int(result.rowcount or 0) > 0


def _column_nullable(model: type[Any], name: str) -> bool:
    column = sa_inspect(model).columns.get(name)
    if column is None:
        return True
    return bool(column.nullable)


def _role_holders(guild: discord.Guild, role_id: int) -> tuple[int, ...]:
    found: list[int] = []
    for member in getattr(guild, "members", ()) or ():
        if bool(getattr(member, "bot", False)):
            continue
        roles = getattr(member, "roles", ()) or ()
        if any(int(getattr(role, "id", 0) or 0) == int(role_id) for role in roles):
            found.append(int(member.id))
    return tuple(found)
