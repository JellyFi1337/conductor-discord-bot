"""Каркас сервера: план, Discord-объекты, привязки, панели."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import monotonic, time
from typing import Any

import discord
import structlog
from sqlalchemy import select

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
    Reputation,
    ReputationTier,
    SelfRoleMenu,
    SelfRoleOption,
    StatsBoard,
    TicketPanel,
    VerificationSettings,
    VoiceHub,
)
from conductor.db.session import session_scope
from conductor.domain.access import Rank
from conductor.domain.achievements import KEY_STAGES
from conductor.domain.create_lock import BUCKET_CHANNEL, BUCKET_ROLE
from conductor.domain.loyalty import LoyaltyTier
from conductor.domain.reputation import default_tiers
from conductor.domain.rights import PERMISSIONS, RightRule, missing_rules, rule_flags
from conductor.domain.rooms import HubPreset
from conductor.domain.seed import (
    INGEST_DEBOUNCE,
    LAYOUT,
    ExistingItem,
    SeedGate,
    SeedMode,
    SeedPlan,
    SeedReport,
    SeedVerdict,
    SkeletonCheck,
    SkeletonEntry,
    SkeletonGate,
    SkeletonKind,
    dependency_order,
    diagnose_status,
    entry_by_key,
    has_seed_permissions,
    has_seed_role_headroom,
    is_same_name,
    layout_keys,
    match_skeleton,
    plan,
    role_order,
    seed_gate,
    seed_guest_plan,
    skeleton_entries,
)
from conductor.domain.self_roles import DEFAULT_GENERAL_OPTIONS, DEFAULT_MENUS, MenuKind
from conductor.domain.showcase import SHOWCASES, ShowcaseState
from conductor.domain.tickets import TicketSettings, dump_ticket_settings, parse_ticket_settings
from conductor.domain.traffic import Lane, request_deadline
from conductor.presentation import copy
from conductor.services.access import RankRoleTaken, cached_snapshot, set_rank_role
from conductor.services.create_lock import continue_after_hold, find_active, hold_for, log_refused, note_success, trip
from conductor.services.guild_config import get_or_create_config, set_role_id
from conductor.services.role_broker import RoleClaim, RoleSubsystem
from conductor.services.traffic import lane

log = structlog.get_logger("conductor.seed")

ProgressFn = Callable[[str, bool], Awaitable[None]]
SEED_REASON = "Conductor seed"
BUILD_TIMEOUT = 180.0


@dataclass
class _BuildSession:
    deadline: float
    objects: dict[str, Any] = field(default_factory=dict)
    existing: dict[str, ExistingItem] = field(default_factory=dict)
    pending: tuple[str, ...] = ()
    report: SeedReport = field(default_factory=lambda: SeedReport(verdict=SeedVerdict.HELD))


_build_session: ContextVar[_BuildSession | None] = ContextVar("seed_build_session", default=None)


_RANK_KEYS: dict[str, Rank] = {
    "role_special": Rank.SPECIAL,
    "role_chief": Rank.CHIEF,
    "role_senior": Rank.SENIOR,
    "role_watcher": Rank.WATCHER,
}
_OLD_KEYS: dict[str, LoyaltyTier] = {
    "role_ruby": LoyaltyTier.RUBY,
    "role_emerald": LoyaltyTier.EMERALD,
    "role_sapphire": LoyaltyTier.SAPPHIRE,
    "role_amethyst": LoyaltyTier.AMETHYST,
}
_LADDER_ATTR: dict[str, str] = {
    "role_guest": "guest_role_id",
    "role_regular": "regular_role_id",
    "role_resident": "resident_role_id",
    "role_friend": "friend_role_id",
}
_REP_INDEX: dict[str, int] = {
    "role_sinner": 0,
    "role_suspicious": 1,
    "role_ordinary": 2,
    "role_reliable": 3,
    "role_respected": 4,
    "role_exemplary": 5,
}
_ACH_KEYS: dict[str, tuple[str, int]] = {
    "role_fixer_1": ("fixer", 1),
    "role_fixer_2": ("fixer", 2),
    "role_fixer_3": ("fixer", 3),
    "role_ideologist_1": ("ideologist", 1),
    "role_ideologist_2": ("ideologist", 2),
    "role_ideologist_3": ("ideologist", 3),
    "role_vigilant_1": ("vigilant", 1),
    "role_vigilant_2": ("vigilant", 2),
    "role_vigilant_3": ("vigilant", 3),
    "role_vigilant_4": ("vigilant", 4),
}
_SUB_TAGS: dict[str, str] = {
    "role_sub_youtube": "youtube",
    "role_sub_polls": "polls",
    "role_sub_announce": "announce",
    "role_sub_lfg": "lfg",
}

__all__ = ["GuildSeed", "SEED_REASON"]

_CENSUS_KEYS = frozenset({"role_special", "role_guest", "role_reliable"})
_GATE_ROLE_KEYS = frozenset({*_RANK_KEYS, "role_verified", "role_mute"})


class GuildSeed:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self.layout = LAYOUT
        self._locks: dict[int, asyncio.Lock] = {}
        self._pending: dict[int, set[str]] = {}
        self._flush: dict[int, asyncio.Task[None]] = {}
        self._busy: set[int] = set()
        self._cancelled_http: set[asyncio.Task[Any]] = set()

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
        return lock

    def cancel_pending(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            for gid, task in list(self._flush.items()):
                if gid in self._busy:
                    continue
                task.cancel()
                self._flush.pop(gid, None)
            self._pending.clear()
            return
        gid = int(guild_id)
        self._pending.pop(gid, None)
        if gid in self._busy:
            return
        task = self._flush.pop(gid, None)
        if task is not None:
            task.cancel()

    def allowed(self, guild_id: int) -> SeedGate:
        serves = getattr(self._bot, "serves", None)
        if callable(serves):
            ok = bool(serves(int(guild_id)))
        else:
            fence = getattr(self._bot, "fence", None)
            ok = bool(getattr(fence, "serves", lambda _gid: False)(int(guild_id)))
        return seed_gate(guild_id=int(guild_id), served=ok)

    def bot_can_seed(self, guild: discord.Guild) -> bool:
        me = getattr(guild, "me", None)
        perms = getattr(me, "guild_permissions", None) if me is not None else None
        if not has_seed_permissions(perms):
            return False
        top = getattr(me, "top_role", None) if me is not None else None
        return has_seed_role_headroom(
            bot_top_position=getattr(top, "position", None),
            blocking_positions=_blocking_positions(guild),
        )

    async def snapshot(self, guild: discord.Guild, *, with_old: bool = False) -> dict[str, ExistingItem]:
        ids = await self._binding_ids(int(guild.id))
        return self._existing_map(guild, ids, with_old=with_old)

    async def preview(self, guild: discord.Guild, *, with_old: bool = False) -> SeedPlan:
        return plan(await self.snapshot(guild, with_old=with_old), with_old=with_old)

    async def catalog(
        self, guild: discord.Guild, *, with_old: bool = False
    ) -> tuple[SeedPlan, dict[str, ExistingItem]]:
        existing = await self.snapshot(guild, with_old=with_old)
        return plan(existing, with_old=with_old), existing

    def schedule_ingest(self, guild: discord.Guild, kind: SkeletonKind, name: str) -> None:
        if self.allowed(int(guild.id)) is not SeedGate.OK:
            return
        entry = match_skeleton(kind, name)
        if entry is None:
            return
        gid = int(guild.id)
        self._pending.setdefault(gid, set()).add(entry.key)
        self._start_flush(guild)

    def _start_flush(self, guild: discord.Guild) -> None:
        gid = int(guild.id)
        current = self._flush.get(gid)
        if current is not None and not current.done():
            return
        try:
            spawn = getattr(self._bot, "spawn", None)
            if callable(spawn):
                task = spawn(self._flush_ingest(guild), name=f"seed-ingest-{gid}")
            else:
                task = asyncio.create_task(self._flush_ingest(guild), name=f"seed-ingest-{gid}")
        except RuntimeError:
            log.exception("seed_ingest_schedule_failed", guild_id=gid)
            return
        self._flush[gid] = task

    async def _flush_ingest(self, guild: discord.Guild) -> None:
        gid = int(guild.id)
        try:
            await asyncio.sleep(INGEST_DEBOUNCE)
        except asyncio.CancelledError:
            return
        try:
            async with self._guild_lock(gid):
                keys = set(self._pending.pop(gid, set()))
                if not keys:
                    return
                if self.allowed(gid) is not SeedGate.OK or not self.bot_can_seed(guild):
                    return
                actor = int(getattr(getattr(self._bot, "user", None), "id", 0) or 0)
                self._busy.add(gid)
                try:
                    await self._safe_execute(
                        guild,
                        actor_id=actor,
                        with_old=True,
                        only_keys=frozenset(keys),
                        refresh=False,
                        mode=SeedMode.ADOPT,
                    )
                finally:
                    self._busy.discard(gid)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("seed_ingest_failed", guild_id=gid)
        finally:
            if self._flush.get(gid) is asyncio.current_task():
                self._flush.pop(gid, None)
            if self._pending.get(gid):
                self._start_flush(guild)

    async def ingest(
        self,
        guild: discord.Guild,
        *,
        kind: SkeletonKind,
        name: str,
        actor_id: int = 0,
    ) -> SeedReport | None:
        entry = match_skeleton(kind, name)
        if entry is None:
            return None
        if self.allowed(int(guild.id)) is not SeedGate.OK:
            return None
        if not self.bot_can_seed(guild):
            return SeedReport(verdict=SeedVerdict.NO_PERMS)
        async with self._guild_lock(int(guild.id)):
            return await self._safe_execute(
                guild,
                actor_id=actor_id,
                with_old=True,
                only_keys=frozenset({entry.key}),
                refresh=False,
                mode=SeedMode.ADOPT,
            )

    async def diagnose_checks(self, guild: discord.Guild) -> tuple[SkeletonCheck, ...]:
        existing = await self.snapshot(guild, with_old=True)
        return tuple(
            diagnose_status(entry, existing.get(entry.key, ExistingItem())) for entry in skeleton_entries(with_old=True)
        )

    async def command_gate(self, guild: discord.Guild, *, with_old: bool = False) -> tuple[SeedPlan | None, str | None]:
        if self.allowed(guild.id) is not SeedGate.OK:
            return None, copy.OWNER_SEED_NOT_SERVED
        try:
            return await self.preview(guild, with_old=with_old), None
        except Exception:
            log.exception("seed_preview_failed", guild_id=int(guild.id))
            return None, copy.SEED_EXECUTE_FAILED

    async def run(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        with_old: bool = False,
        mode: SeedMode = SeedMode.BUILD,
        on_progress: ProgressFn | None = None,
        only_keys: frozenset[str] | None = None,
        max_duration: float = BUILD_TIMEOUT,
    ) -> SeedReport:
        if self.allowed(guild.id) is not SeedGate.OK:
            return SeedReport(verdict=SeedVerdict.NOT_SERVED)
        duration = max(0.0, min(BUILD_TIMEOUT, float(max_duration)))
        session = _BuildSession(monotonic() + duration) if mode is SeedMode.BUILD else None
        token = _build_session.set(session)
        deadline_token = request_deadline.set(None if session is None else session.deadline)
        try:
            # Ожидание другой сборки также входит в общий бюджет команды.
            async with asyncio.timeout(duration if session is not None else None):
                async with self._guild_lock(int(guild.id)):
                    self.cancel_pending(int(guild.id))
                    try:
                        with lane(Lane.BACKGROUND):
                            return await self._safe_execute(
                                guild,
                                actor_id=actor_id,
                                with_old=with_old,
                                on_progress=on_progress,
                                mode=mode,
                                only_keys=only_keys,
                            )
                    except asyncio.CancelledError:
                        # Только БД: сохранить уже полученные ID до освобождения замка.
                        # Новые Discord-запросы после истечения бюджета не выполняются.
                        if session is not None and session.objects:
                            notes = list(session.report.notes)
                            with suppress(TimeoutError):
                                async with asyncio.timeout(5.0):
                                    bound = await self._persist(
                                        guild, session.objects, session.existing, actor_id=actor_id, notes=notes
                                    )
                                    session.report = replace(session.report, bound=tuple(bound), notes=tuple(notes))
                        raise
        except TimeoutError:
            assert session is not None
            return replace(
                session.report,
                verdict=SeedVerdict.PARTIAL,
                held=tuple(key for key in session.pending if key not in session.objects),
                notes=(*session.report.notes, copy.SEED_TIME_BUDGET),
            )
        finally:
            request_deadline.reset(deadline_token)
            _build_session.reset(token)

    async def bind_existing(self, guild: discord.Guild, ids: Mapping[str, int], *, actor_id: int) -> SeedReport:
        """Привязать существующие объекты по id, не переименовывая."""
        if self.allowed(guild.id) is not SeedGate.OK:
            return SeedReport(verdict=SeedVerdict.NOT_SERVED)
        objects: dict[str, Any] = {}
        for key, object_id in ids.items():
            entry = entry_by_key(key)
            if entry is None:
                continue
            obj = _lookup(guild, entry, int(object_id))
            if obj is not None:
                objects[key] = obj
        async with self._guild_lock(int(guild.id)):
            notes: list[str] = []
            bound = await self._persist(guild, objects, {}, actor_id=actor_id, notes=notes)
            if not objects:
                return SeedReport(verdict=SeedVerdict.OK, bound=tuple(bound), notes=tuple(notes))
            return await self._safe_execute(
                guild,
                actor_id=actor_id,
                with_old=True,
                only_keys=frozenset(objects),
                refresh=False,
                mode=SeedMode.ADOPT,
            )

    async def repair(
        self, guild: discord.Guild, entry_key: str, *, actor_id: int, mode: SeedMode = SeedMode.BUILD
    ) -> SeedReport:
        if self.allowed(guild.id) is not SeedGate.OK:
            return SeedReport(verdict=SeedVerdict.NOT_SERVED)
        entry = entry_by_key(entry_key)
        if entry is None:
            return SeedReport(verdict=SeedVerdict.FAILED, notes=(copy.SEED_MISSING_ROW,))
        async with self._guild_lock(int(guild.id)):
            return await self._safe_execute(
                guild,
                actor_id=actor_id,
                with_old=True,
                only_keys=frozenset({entry.key}),
                refresh=False,
                mode=mode,
            )

    async def reapply_rights(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        keys: frozenset[str] | None = None,
    ) -> SeedReport:
        """Пересобрать overwrites без создания и без привязок. force = keys или все ключи."""
        if self.allowed(guild.id) is not SeedGate.OK:
            return SeedReport(verdict=SeedVerdict.NOT_SERVED)
        async with self._guild_lock(int(guild.id)):
            existing = await self.snapshot(guild, with_old=True)
            objects: dict[str, Any] = {}
            force: set[str] = set()
            for entry in skeleton_entries(with_old=True):
                item = existing.get(entry.key, ExistingItem())
                obj = _resolve_object(guild, entry, item)
                if obj is None and item.exists:
                    obj = _named(guild, entry)
                if obj is None:
                    continue
                objects[entry.key] = obj
                if entry.kind is SkeletonKind.ROLE:
                    continue
                if keys is None or entry.key in keys:
                    force.add(entry.key)
            scoped: dict[str, Any] = {}
            for key, obj in objects.items():
                if key in force:
                    scoped[key] = obj
                    continue
                found = entry_by_key(key)
                if found is not None and found.kind is SkeletonKind.ROLE:
                    scoped[key] = obj
            notes: list[str] = []
            log.info(
                "reapply_rights",
                guild_id=int(guild.id),
                actor_id=int(actor_id),
                keys=sorted(force),
            )
            changed = await self._apply_rights(guild, scoped, existing, notes, force=frozenset(force))
            return SeedReport(verdict=SeedVerdict.OK, repaired=changed, notes=tuple(notes))

    async def _safe_execute(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        with_old: bool,
        on_progress: ProgressFn | None = None,
        only_keys: frozenset[str] | None = None,
        refresh: bool = True,
        mode: SeedMode = SeedMode.BUILD,
    ) -> SeedReport:
        try:
            return await self._execute(
                guild,
                actor_id=actor_id,
                with_old=with_old,
                on_progress=on_progress,
                only_keys=only_keys,
                refresh=refresh,
                mode=mode,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("seed_execute_failed", guild_id=int(guild.id))
            return SeedReport(verdict=SeedVerdict.FAILED, notes=(copy.SEED_EXECUTE_FAILED,))

    async def _execute(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        with_old: bool,
        on_progress: ProgressFn | None = None,
        only_keys: frozenset[str] | None = None,
        refresh: bool = True,
        mode: SeedMode = SeedMode.BUILD,
    ) -> SeedReport:
        notes: list[str] = []
        by_name: list[str] = []

        async def mark(name: str, before: int) -> None:
            if on_progress is None:
                return
            try:
                await on_progress(name, len(notes) == before)
            except Exception:
                log.exception("seed_progress_failed", guild_id=int(guild.id))

        if not self.bot_can_seed(guild):
            if on_progress is not None:
                try:
                    await on_progress(copy.OWNER_SEED_STAGES[0], False)
                except Exception:
                    log.exception("seed_progress_failed", guild_id=int(guild.id))
            return SeedReport(verdict=SeedVerdict.NO_PERMS)
        await mark(copy.OWNER_SEED_STAGES[0], len(notes))

        if refresh:
            await self._refresh_guild(guild)
        ids = await self._binding_ids(int(guild.id))
        existing = self._existing_map(guild, ids, with_old=with_old or only_keys is not None)
        wanted = plan(existing, with_old=with_old or only_keys is not None)
        if only_keys is not None:
            wanted = SeedPlan(
                create=tuple(key for key in wanted.create if key in only_keys),
                reuse=tuple(key for key in wanted.reuse if key in only_keys),
                repair=tuple(key for key in wanted.repair if key in only_keys),
                by_name=tuple(key for key in wanted.by_name if key in only_keys),
            )
            if not wanted.by_name and not wanted.repair and not wanted.create:
                return SeedReport(verdict=SeedVerdict.OK, reused=len(wanted.reuse))

        objects: dict[str, Any] = {}
        created = reused = repaired = 0
        build = _build_session.get()
        if build is not None:
            build.objects = objects
            build.existing = existing
            build.pending = tuple(wanted.create)
        created_roles = reused_roles = created_channels = reused_channels = created_categories = 0
        missing: list[str] = []
        held: list[str] = []
        failed: list[str] = []
        skip_reuse = set(wanted.reuse) - set(wanted.by_name)

        before = len(notes)
        for key in dependency_order((*wanted.reuse, *wanted.repair, *wanted.create)):
            if key in skip_reuse:
                continue
            entry = entry_by_key(key)
            if entry is None:
                continue
            obj, action = await self._ensure(
                guild, entry, existing.get(key, ExistingItem()), objects, existing, notes, mode=mode
            )
            if obj is None:
                if action == "held":
                    held.append(key)
                elif action == "failed":
                    failed.append(key)
                elif not entry.optional:
                    missing.append(key)
                continue
            objects[key] = obj
            counted = _count(
                entry,
                action,
                created,
                reused,
                repaired,
                created_roles,
                reused_roles,
                created_channels,
                reused_channels,
                created_categories,
            )
            created = counted[0]
            reused = counted[1]
            repaired = counted[2]
            created_roles = counted[3]
            reused_roles = counted[4]
            created_channels = counted[5]
            reused_channels = counted[6]
            created_categories = counted[7]
            if build is not None:
                build.report = SeedReport(
                    verdict=SeedVerdict.PARTIAL,
                    created=created,
                    reused=reused,
                    repaired=repaired,
                    created_roles=created_roles,
                    reused_roles=reused_roles,
                    created_channels=created_channels,
                    reused_channels=reused_channels,
                    created_categories=created_categories,
                    notes=tuple(notes),
                    failed=tuple(failed),
                    missing=tuple(missing),
                )
            if action == "by_name":
                by_name.append(key)
        if objects.keys() & _GATE_ROLE_KEYS:
            for key, item in existing.items():
                if key in objects:
                    continue
                entry = entry_by_key(key)
                if entry is None or entry.kind is SkeletonKind.ROLE:
                    continue
                if item.overwrites_ok:
                    continue
                obj = _resolve_object(guild, entry, item) or _named(guild, entry)
                if obj is not None:
                    objects[key] = obj
        if only_keys is not None:
            for key in missing:
                entry = entry_by_key(key)
                if entry is not None:
                    notes.append(copy.seed_manual_one(entry.diagnose_label))
        elif missing and not held:
            notes.append(copy.seed_manual_missing(len(missing)))
        await mark(copy.OWNER_SEED_STAGES[1], before)

        before = len(notes)
        rights_scope = dict(objects)
        forced = {*wanted.by_name, *wanted.create, *wanted.repair}
        # Роль-ворота с новым id: у стоящих каналов её overwrite ещё нет, а overwrites_ok
        # считался без этой роли. Пересобрать права по всем привязанным каналам.
        if objects.keys() & _GATE_ROLE_KEYS & {*wanted.create, *wanted.by_name}:
            for key, item in existing.items():
                entry = entry_by_key(key)
                if key in rights_scope or entry is None or entry.kind is SkeletonKind.ROLE:
                    continue
                if not (item.bound_id and item.exists):
                    continue
                obj = _resolve_object(guild, entry, item)
                if obj is not None:
                    rights_scope[key] = obj
                    forced.add(key)
        await self._apply_rights(
            guild,
            rights_scope,
            existing,
            notes,
            force=frozenset(forced),
        )
        align_roles = _align_targets(guild, objects, existing, wanted, with_old=with_old or only_keys is not None)
        await self._align_positions(
            guild,
            align_roles,
            with_old=with_old or only_keys is not None,
            notes=notes,
            skip=not align_roles,
        )
        await mark(copy.OWNER_SEED_STAGES[2], before)

        before = len(notes)
        bound = await self._persist(guild, objects, existing, actor_id=actor_id, notes=notes)
        await self._side_effects(guild, objects, actor_id=actor_id, notes=notes)
        await mark(copy.OWNER_SEED_STAGES[3], before)

        before = len(notes)
        await self._panels(guild, objects, actor_id=actor_id, notes=notes, only_keys=only_keys)
        await mark(copy.OWNER_SEED_STAGES[4], before)

        before = len(notes)
        await self._data(guild, objects, notes)
        await mark(copy.OWNER_SEED_STAGES[5], before)

        special_granted = False
        humans = 0
        census_hit = {
            key
            for key in _CENSUS_KEYS
            if key in objects
            and (
                key in wanted.by_name
                or not (existing.get(key, ExistingItem()).bound_id and existing.get(key, ExistingItem()).exists)
            )
        }
        if census_hit:
            before = len(notes)
            special_granted, humans = await self._census(guild, objects, notes)
            await mark(copy.OWNER_SEED_STAGES[6], before)
        elif only_keys is None:
            await mark(copy.OWNER_SEED_STAGES[6], before)

        hints: list[str] = []
        if only_keys is None and objects.get("cat_server") is not None:
            hints.append(copy.OWNER_SEED_SERVER_HINT)
        if created == 0 and held and not failed:
            verdict = SeedVerdict.HELD
        elif notes:
            verdict = SeedVerdict.PARTIAL
        else:
            verdict = SeedVerdict.OK
        return SeedReport(
            verdict=verdict,
            created=created,
            reused=reused,
            repaired=repaired,
            created_roles=created_roles,
            reused_roles=reused_roles,
            created_channels=created_channels,
            reused_channels=reused_channels,
            created_categories=created_categories,
            bound=tuple(dict.fromkeys(bound)),
            notes=tuple([*notes, *hints]),
            special_granted=special_granted,
            humans=humans,
            by_name=tuple(by_name),
            missing=tuple(missing),
            held=tuple(held),
            failed=tuple(failed),
        )

    async def _binding_ids(self, guild_id: int) -> dict[str, int | None]:
        ids: dict[str, int | None] = {entry.key: None for entry in SKELETON_KEYS}
        async with session_scope(self._bot.sessions) as session:
            for row in (await session.scalars(select(GuildLayout).where(GuildLayout.guild_id == guild_id))).all():
                if row.key in ids:
                    ids[row.key] = int(row.channel_id)
            config = await session.get(GuildConfig, guild_id)
            if config is not None:
                ids["user_logs"] = config.user_log_channel_id
                ids["admin_logs"] = config.admin_log_channel_id
                ids["staff_feed"] = config.staff_feed_channel_id
                ids["automod"] = config.automod_channel_id
                ids["structure"] = config.structure_channel_id
                ids["role_ruby"] = config.ruby_role_id
                ids["role_emerald"] = config.emerald_role_id
                ids["role_sapphire"] = config.sapphire_role_id
                ids["role_amethyst"] = config.amethyst_role_id
            for row in (await session.scalars(select(GuildRank).where(GuildRank.guild_id == guild_id))).all():
                key = next((item for item, rank in _RANK_KEYS.items() if rank.key == row.rank), None)
                if key is not None and ids.get(key) is None:
                    ids[key] = int(row.role_id)
            verify = await session.get(VerificationSettings, guild_id)
            if verify is not None:
                ids["verify"] = verify.channel_id
                ids["rules"] = verify.rules_channel_id or ids.get("rules")
                ids["role_verified"] = verify.verified_role_id
            menus = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.guild_id == guild_id))).all()
            for menu in menus:
                if menu.channel_id:
                    ids["roles"] = int(menu.channel_id)
                    break
            for row in (await session.scalars(select(SelfRoleOption).where(SelfRoleOption.guild_id == guild_id))).all():
                tag = str(row.tag or "")
                key = next((item for item, wanted in _SUB_TAGS.items() if wanted == tag), None)
                if key is not None:
                    ids[key] = int(row.role_id)
            lfg = await session.get(LfgSettings, guild_id)
            if lfg is not None:
                ids["lfg"] = lfg.channel_id
            poll = await session.get(PollSettings, guild_id)
            if poll is not None:
                ids["polls"] = poll.channel_id
            stats = await session.get(StatsBoard, guild_id)
            if stats is not None:
                ids["stats"] = stats.channel_id
            panel = await session.scalar(
                select(TicketPanel).where(TicketPanel.guild_id == guild_id).order_by(TicketPanel.id)
            )
            if panel is not None:
                ids["tickets"] = int(panel.channel_id)
                settings = parse_ticket_settings(panel.settings)
                ids["ticket_archive"] = settings.archive_channel_id
                ids["ticket_log"] = settings.log_channel_id
                if settings.discord_category_id:
                    ids["cat_tickets"] = settings.discord_category_id
            hub = await session.get(VoiceHub, guild_id)
            if hub is not None:
                ids["hub"] = int(hub.channel_id)
                if hub.category_id:
                    ids["cat_rooms"] = int(hub.category_id)
            groups = await session.get(GroupHub, guild_id)
            if groups is not None:
                ids["cat_groups"] = int(groups.category_id)
                if groups.archive_category_id:
                    ids["cat_group_archive"] = int(groups.archive_category_id)
            mod = await session.get(ModSettings, guild_id)
            if mod is not None:
                ids["role_mute"] = mod.mute_role_id
                if mod.evidence_category_id:
                    ids["cat_evidence"] = int(mod.evidence_category_id)
                if mod.evidence_archive_channel_id:
                    ids["evidence_archive"] = int(mod.evidence_archive_channel_id)
            ladder = await session.get(LadderSettings, guild_id)
            if ladder is not None:
                ids["role_guest"] = ladder.guest_role_id
                ids["role_regular"] = ladder.regular_role_id
                ids["role_resident"] = ladder.resident_role_id
                ids["role_friend"] = ladder.friend_role_id
            for row in (await session.scalars(select(ReputationTier).where(ReputationTier.guild_id == guild_id))).all():
                key = next((item for item, index in _REP_INDEX.items() if index == int(row.tier)), None)
                if key is not None and row.role_id:
                    ids[key] = int(row.role_id)
            for row in (await session.scalars(select(AchievementDef).where(AchievementDef.guild_id == guild_id))).all():
                key = next(
                    (item for item, pair in _ACH_KEYS.items() if pair == (row.key, int(row.stage))),
                    None,
                )
                if key is not None and row.role_id:
                    ids[key] = int(row.role_id)
        return ids

    def _existing_map(
        self,
        guild: discord.Guild,
        ids: MappingIds,
        *,
        with_old: bool,
    ) -> dict[str, ExistingItem]:
        found: dict[str, ExistingItem] = {}
        positions = _role_positions_ok(guild, ids, with_old=with_old)
        for entry in skeleton_entries(with_old=with_old):
            bound = _as_int(ids.get(entry.key))
            obj = _lookup(guild, entry, bound)
            if bound is not None:
                exists = obj is not None
                found_by_name = False
                found_id = bound if exists else None
            else:
                obj = _named(guild, entry)
                exists = obj is not None
                found_by_name = exists
                found_id = None if obj is None else int(obj.id)
            parent_ok = True
            if entry.category and obj is not None:
                parent = _lookup(guild, entry_by_key(entry.category), _as_int(ids.get(entry.category)))
                if parent is None:
                    parent = _named(guild, entry_by_key(entry.category)) if entry_by_key(entry.category) else None
                if parent is not None:
                    parent_ok = int(getattr(obj, "category_id", 0) or 0) == int(parent.id)
            overwrites_ok = True
            colour_ok = True
            hoist_ok = True
            if obj is not None and entry.kind is not SkeletonKind.ROLE:
                overwrites_ok = _overwrites_match(guild, obj, entry, ids)
            if obj is not None and entry.kind is SkeletonKind.ROLE:
                colour = int(getattr(getattr(obj, "colour", None), "value", 0) or 0)
                colour_ok = colour == int(entry.colour)
                hoist_ok = bool(getattr(obj, "hoist", True)) == bool(entry.hoist)
            found[entry.key] = ExistingItem(
                bound_id=bound,
                exists=exists,
                name=str(getattr(obj, "name", "") or ""),
                category_id=getattr(obj, "category_id", None) if obj is not None else None,
                overwrites_ok=overwrites_ok,
                position_ok=positions.get(entry.key, True),
                category_ok=parent_ok,
                found_by_name=found_by_name,
                found_id=found_id,
                colour_ok=colour_ok,
                hoist_ok=hoist_ok,
            )
        return found

    async def _ensure(
        self,
        guild: discord.Guild,
        entry: SkeletonEntry,
        item: ExistingItem,
        objects: dict[str, Any],
        existing: dict[str, ExistingItem],
        notes: list[str],
        *,
        mode: SeedMode,
    ) -> tuple[Any, str]:
        current = _resolve_object(guild, entry, item) or _named(guild, entry)
        if current is not None:
            if mode is SeedMode.PLAN:
                return current, "reuse"
            action = "by_name" if item.bound_id is None or (item.found_by_name and not item.bound_id) else "reuse"
            if entry.kind is SkeletonKind.ROLE:
                if await self._repair_role(current, entry, notes) and action == "reuse":
                    action = "repair"
                return current, action
            await self._repair_channel(guild, current, entry, objects, existing, notes)
            if (not item.overwrites_ok or not item.category_ok) and action != "by_name":
                action = "repair"
            return current, action
        if mode is SeedMode.BUILD and entry.creatable:
            return await self._create_missing(guild, entry, objects, existing, notes)
        return None, "missing"

    async def _create_missing(
        self,
        guild: discord.Guild,
        entry: SkeletonEntry,
        objects: dict[str, Any],
        existing: dict[str, ExistingItem],
        notes: list[str],
    ) -> tuple[Any, str]:
        build = _build_session.get()
        bucket = BUCKET_ROLE if entry.kind is SkeletonKind.ROLE else BUCKET_CHANNEL
        while True:
            before = len(notes)
            result = await self._create_missing_once(guild, entry, objects, existing, notes)
            if result[1] != "held" or build is None:
                return result
            hold = find_active(self._bot, guild.id, (bucket,), time())
            wait = 0.0 if hold is None else hold.remaining(time()) + 0.05
            if wait <= 0 or wait + 5.0 >= build.deadline - monotonic():
                return result
            # Не переносим в итоговый отчёт уже разрешённое временное ограничение.
            del notes[before:]
            await asyncio.sleep(wait)

    async def _create_missing_once(
        self,
        guild: discord.Guild,
        entry: SkeletonEntry,
        objects: dict[str, Any],
        existing: dict[str, ExistingItem],
        notes: list[str],
    ) -> tuple[Any, str]:
        parent = None
        if entry.category:
            parent = _object_for(guild, entry.category, objects, existing)
            if parent is None:
                notes.append(copy.seed_parent_missing(entry.diagnose_label))
                return None, "blocked_parent"
        if entry.kind is SkeletonKind.ROLE and not has_seed_role_headroom(
            bot_top_position=_bot_top_position(guild),
            blocking_positions=_blocking_positions(guild),
        ):
            notes.append(copy.SEED_ROLE_SPACE)
            return None, "failed"
        bucket = BUCKET_ROLE if entry.kind is SkeletonKind.ROLE else BUCKET_CHANNEL
        hold = await hold_for(self._bot, guild.id, bucket)
        if hold is not None and _build_session.get() is None:
            waiter = asyncio.Event()

            async def _mark() -> None:
                waiter.set()

            queued = continue_after_hold(self._bot, ("seed-create-defer", guild.id, entry.key), hold, _mark)
            running = bool(getattr(getattr(self._bot, "queue", None), "_running", False))
            if queued and running:
                await waiter.wait()
                hold = await hold_for(self._bot, guild.id, bucket)
        if hold is not None:
            log_refused(guild.id, bucket, hold, action=bucket)
            if copy.SEED_CREATE_HELD not in notes:
                notes.append(copy.SEED_CREATE_HELD)
                notes.append(copy.hold_text(
                    str(hold.source),
                    hold.retry_after,
                    action=copy.hold_action(bucket),
                    route=hold.route,
                ))
            return None, "held"
        try:
            with lane(Lane.BACKGROUND if _build_session.get() is not None else Lane.STAFF):
                if entry.kind is SkeletonKind.CATEGORY:
                    current = await self._discord(guild.create_category(entry.name, reason=SEED_REASON))
                elif entry.kind is SkeletonKind.TEXT:
                    current = await self._discord(
                        guild.create_text_channel(entry.name, category=parent, topic=entry.topic, reason=SEED_REASON)
                    )
                elif entry.kind is SkeletonKind.VOICE:
                    current = await self._discord(
                        guild.create_voice_channel(entry.name, category=parent, reason=SEED_REASON)
                    )
                elif entry.kind is SkeletonKind.ROLE:
                    current = await self._discord(
                        guild.create_role(
                            name=entry.name,
                            colour=discord.Colour(entry.colour),
                            hoist=entry.hoist,
                            reason=SEED_REASON,
                        )
                    )
                else:
                    return None, "missing"
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild.id, bucket, exc)
            if hold is not None:
                log_refused(guild.id, bucket, hold, action=bucket)
                notes.append(copy.hold_text(
                    str(hold.source), hold.retry_after, action=copy.hold_action(bucket), route=hold.route
                ))
                return None, "held"
            notes.append(copy.seed_create_failed(entry.diagnose_label, exc))
            return None, "failed"
        note_success(self._bot, guild.id, bucket)
        return current, "create"

    async def _repair_role(self, role: Any, entry: SkeletonEntry, notes: list[str]) -> bool:
        colour = int(getattr(getattr(role, "colour", None), "value", 0) or 0)
        hoist = bool(getattr(role, "hoist", True))
        payload: dict[str, Any] = {}
        if colour != int(entry.colour):
            payload["colour"] = discord.Colour(entry.colour)
        if hoist != bool(entry.hoist):
            payload["hoist"] = entry.hoist
        if not payload:
            return False
        edit = getattr(role, "edit", None)
        if not callable(edit):
            return False
        try:
            await self._discord(edit(**payload, reason=SEED_REASON))
            if "colour" in payload:
                role.colour = payload["colour"]
            if "hoist" in payload:
                role.hoist = entry.hoist
            return True
        except Exception as exc:
            notes.append(f"{entry.diagnose_label}: {exc}")
            return False

    async def _repair_channel(
        self,
        guild: discord.Guild,
        channel: Any,
        entry: SkeletonEntry,
        objects: dict[str, Any],
        existing: dict[str, ExistingItem],
        notes: list[str],
    ) -> None:
        payload: dict[str, Any] = {}
        parent = _object_for(guild, entry.category or "", objects, existing) if entry.category else None
        if parent is not None and int(getattr(channel, "category_id", 0) or 0) != int(parent.id):
            payload["category"] = parent
        if entry.topic and str(getattr(channel, "topic", "") or "") != entry.topic:
            payload["topic"] = entry.topic
        if not payload:
            return
        edit = getattr(channel, "edit", None)
        if not callable(edit):
            return
        try:
            await self._discord(edit(**payload, reason=SEED_REASON))
            if "category" in payload:
                channel.category_id = int(parent.id)
                channel.category = parent
            if "topic" in payload:
                channel.topic = entry.topic
        except Exception as exc:
            notes.append(f"{entry.diagnose_label}: {exc}")

    async def _apply_rights(
        self,
        guild: discord.Guild,
        objects: dict[str, Any],
        existing: dict[str, ExistingItem],
        notes: list[str],
        *,
        force: frozenset[str] = frozenset(),
    ) -> int:
        """Для каждой строки: спросить missing_rules и применить только недостающее."""
        actors = _right_actors(guild, objects=objects, existing=existing)
        changed = 0
        for entry in skeleton_entries(with_old=True):
            channel = objects.get(entry.key)
            if channel is None or entry.kind is SkeletonKind.ROLE:
                continue
            item = existing.get(entry.key, ExistingItem())
            if entry.key not in force and item.overwrites_ok and item.category_ok:
                continue
            missing = missing_rules(entry, _actual_rights(channel, actors))
            if not missing:
                continue
            for rule in missing:
                for actor in actors.get(rule.target, ()):
                    if await self._put_missing(guild, channel, actor, rule, notes):
                        changed += 1
        return changed

    async def _put_missing(
        self,
        guild: discord.Guild,
        channel: Any,
        actor: Any,
        rule: RightRule,
        notes: list[str],
    ) -> bool:
        current = _overwrite_for(channel, actor)
        needed = {name: wanted for name, wanted in rule_flags(rule).items() if _flag(current, name) is not wanted}
        if not needed:
            return False
        return await self._overwrite(guild, channel, actor, _merged_overwrite(current, needed), notes)

    async def _overwrite(
        self,
        guild: discord.Guild,
        channel: Any,
        target: Any,
        overwrite: discord.PermissionOverwrite,
        notes: list[str],
    ) -> bool:
        setter = getattr(channel, "set_permissions", None)
        if not callable(setter):
            return False
        try:
            await self._discord(setter(target, overwrite=overwrite, reason=SEED_REASON))
        except discord.Forbidden as exc:
            if _bot_role_below(guild, target):
                notes.append(copy.rights_bot_below(channel, target))
            else:
                notes.append(copy.seed_rights(getattr(channel, "name", getattr(channel, "id", "?")), exc))
            return False
        except Exception as exc:
            notes.append(copy.seed_rights(getattr(channel, "name", getattr(channel, "id", "?")), exc))
            return False
        return True

    async def _align_positions(
        self,
        guild: discord.Guild,
        objects: dict[str, Any],
        *,
        with_old: bool,
        notes: list[str],
        skip: bool = False,
    ) -> None:
        if skip:
            return
        top = _bot_top_position(guild)
        if top is None:
            return
        order = [objects[key] for key in role_order(with_old=with_old) if key in objects]
        if not order:
            return
        positions = {role: int(top) - 1 - index for index, role in enumerate(order)}
        if any(position <= 0 for position in positions.values()):
            notes.append(copy.SEED_ROLE_SPACE)
            return
        edit = getattr(guild, "edit_role_positions", None)
        if not callable(edit):
            return
        try:
            await self._discord(edit(positions, reason=SEED_REASON))
        except TypeError:
            try:
                await self._discord(edit(positions))
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_ROLE_POS, exc))
        except Exception as exc:
            notes.append(copy.seed_note(copy.SEED_ROLE_POS, exc))

    async def _persist(
        self,
        guild: discord.Guild,
        objects: dict[str, Any],
        existing: dict[str, ExistingItem],
        *,
        actor_id: int,
        notes: list[str],
    ) -> list[str]:
        bound: list[str] = []
        gid = int(guild.id)

        def take(key: str) -> int | None:
            obj = objects.get(key)
            item = existing.get(key, ExistingItem())
            if item.bound_id and item.exists:
                return int(item.bound_id)
            if obj is not None:
                return int(obj.id)
            return _as_int(item.found_id)

        try:
            async with session_scope(self._bot.sessions) as session:
                # Ключи каркаса без своей настройки: список вычисляется из SKELETON,
                # чтобы новая строка привязывалась без правки этого места.
                for key in layout_keys():
                    channel_id = take(key)
                    if channel_id is None:
                        continue
                    item = existing.get(key, ExistingItem())
                    if item.bound_id and item.exists and int(item.bound_id) == int(channel_id):
                        bound.append(key)
                        continue
                    await _upsert_layout(session, gid, key, channel_id)
                    bound.append(key)
                config = await get_or_create_config(session, gid)
                for key, attr in (
                    ("user_logs", "user_log_channel_id"),
                    ("admin_logs", "admin_log_channel_id"),
                    ("staff_feed", "staff_feed_channel_id"),
                    ("automod", "automod_channel_id"),
                    ("structure", "structure_channel_id"),
                ):
                    channel_id = take(key)
                    if channel_id is None:
                        continue
                    current = getattr(config, attr)
                    item = existing.get(key, ExistingItem())
                    if current and item.exists:
                        bound.append(key)
                        continue
                    setattr(config, attr, channel_id)
                    bound.append(key)
                for key, tier in _OLD_KEYS.items():
                    role_id = take(key)
                    if role_id is None:
                        continue
                    item = existing.get(key, ExistingItem())
                    if item.bound_id and item.exists:
                        bound.append(key)
                        continue
                    set_role_id(config, tier, role_id)
                    bound.append(key)
                for key, rank in _RANK_KEYS.items():
                    role_id = take(key)
                    if role_id is None:
                        continue
                    item = existing.get(key, ExistingItem())
                    if not (item.bound_id and item.exists):
                        try:
                            await set_rank_role(session, gid, rank, role_id)
                        except RankRoleTaken as exc:
                            # Роль, найденная по имени, уже привязана к другому рангу — это заметка,
                            # а не откат всех остальных привязок.
                            notes.append(copy.seed_note(copy.SEED_BIND, exc))
                            continue
                    bound.append(key)
                verify = await session.get(VerificationSettings, gid)
                if verify is None:
                    verify = VerificationSettings(guild_id=gid)
                    session.add(verify)
                for key, attr in (
                    ("verify", "channel_id"),
                    ("rules", "rules_channel_id"),
                    ("role_verified", "verified_role_id"),
                ):
                    value = take(key)
                    if value is None:
                        continue
                    item = existing.get(key, ExistingItem())
                    if getattr(verify, attr) and item.exists:
                        bound.append(key)
                        continue
                    setattr(verify, attr, value)
                    bound.append(key)
                menus = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.guild_id == gid))).all()
                if not menus:
                    for kind, title in DEFAULT_MENUS:
                        session.add(SelfRoleMenu(guild_id=gid, kind=kind.value, title=title))
                    await session.flush()
                    menus = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.guild_id == gid))).all()
                roles_channel = take("roles")
                if roles_channel is not None:
                    item = existing.get("roles", ExistingItem())
                    for menu in menus:
                        if menu.channel_id and item.exists:
                            continue
                        menu.channel_id = roles_channel
                    bound.append("roles")
                general = next((menu for menu in menus if menu.kind == MenuKind.GENERAL.value), None)
                if general is not None:
                    have_tags = {
                        str(row.tag)
                        for row in (
                            await session.scalars(select(SelfRoleOption).where(SelfRoleOption.menu_id == general.id))
                        ).all()
                    }
                    labels = {tag.value: label for label, tag in DEFAULT_GENERAL_OPTIONS}
                    for key, tag in _SUB_TAGS.items():
                        role_id = take(key)
                        if role_id is None or tag in have_tags:
                            if role_id is not None:
                                bound.append(key)
                            continue
                        session.add(
                            SelfRoleOption(
                                menu_id=int(general.id),
                                guild_id=gid,
                                role_id=role_id,
                                label=labels.get(tag, tag),
                                description="",
                                emoji=None,
                                tag=tag,
                                position=len(have_tags),
                            )
                        )
                        have_tags.add(tag)
                        bound.append(key)
                lfg = await session.get(LfgSettings, gid)
                if lfg is None:
                    lfg = LfgSettings(guild_id=gid)
                    session.add(lfg)
                channel_id = take("lfg")
                if channel_id is not None:
                    item = existing.get("lfg", ExistingItem())
                    if not (lfg.channel_id and item.exists):
                        lfg.channel_id = channel_id
                    bound.append("lfg")
                poll = await session.get(PollSettings, gid)
                if poll is None:
                    poll = PollSettings(guild_id=gid)
                    session.add(poll)
                channel_id = take("polls")
                if channel_id is not None:
                    item = existing.get("polls", ExistingItem())
                    if not (poll.channel_id and item.exists):
                        poll.channel_id = channel_id
                    bound.append("polls")
                ping = take("role_sub_polls")
                if ping is not None and not poll.ping_role_id:
                    poll.ping_role_id = ping
                stats = await session.get(StatsBoard, gid)
                if stats is None:
                    stats = StatsBoard(guild_id=gid)
                    session.add(stats)
                channel_id = take("stats")
                if channel_id is not None:
                    item = existing.get("stats", ExistingItem())
                    if not (stats.channel_id and item.exists):
                        stats.channel_id = channel_id
                    bound.append("stats")
                tickets = take("tickets")
                archive = take("ticket_archive")
                log_channel = take("ticket_log")
                category = take("cat_tickets")
                panel = await session.scalar(
                    select(TicketPanel).where(TicketPanel.guild_id == gid).order_by(TicketPanel.id)
                )
                if tickets is not None:
                    item = existing.get("tickets", ExistingItem())
                    settings = TicketSettings() if panel is None else parse_ticket_settings(panel.settings)
                    archive_id = settings.archive_channel_id
                    if archive is not None and not (
                        archive_id and existing.get("ticket_archive", ExistingItem()).exists
                    ):
                        archive_id = archive
                    log_id = settings.log_channel_id
                    if log_channel is not None and not (log_id and existing.get("ticket_log", ExistingItem()).exists):
                        log_id = log_channel
                    cat_id = settings.discord_category_id
                    if category is not None and not (cat_id and existing.get("cat_tickets", ExistingItem()).exists):
                        cat_id = category
                    dumped = dump_ticket_settings(
                        TicketSettings(
                            open_limit=settings.open_limit,
                            author_may_close=settings.author_may_close,
                            open_mode=settings.open_mode,
                            archive_channel_id=archive_id,
                            discord_category_id=cat_id,
                            log_channel_id=log_id,
                            autoclose_hours=settings.autoclose_hours,
                            reopen_days=settings.reopen_days,
                        )
                    )
                    if panel is None:
                        session.add(
                            TicketPanel(
                                guild_id=gid,
                                channel_id=tickets,
                                settings=dumped,
                                created_at=datetime.now(UTC),
                                created_by=int(actor_id),
                            )
                        )
                    else:
                        if not (panel.channel_id and item.exists):
                            panel.channel_id = tickets
                        panel.settings = dumped
                    bound.extend(("tickets", "ticket_archive", "ticket_log"))
                hub_id = take("hub")
                rooms_cat = take("cat_rooms")
                if hub_id is not None:
                    item = existing.get("hub", ExistingItem())
                    hub = await session.get(VoiceHub, gid)
                    if hub is None:
                        session.add(VoiceHub(guild_id=gid, channel_id=hub_id, category_id=rooms_cat))
                    elif not (item.bound_id and item.exists):
                        hub.channel_id = hub_id
                        if rooms_cat is not None:
                            hub.category_id = rooms_cat
                    bound.append("hub")
                    rooms = getattr(self._bot, "rooms", None)
                    setter = getattr(rooms, "set_hub", None)
                    if callable(setter):
                        try:
                            await setter(session, gid, channel_id=hub_id, category_id=rooms_cat)
                        except Exception as exc:
                            notes.append(copy.seed_note(copy.SEED_HUB, exc))
                groups_cat = take("cat_groups")
                if groups_cat is not None:
                    item = existing.get("cat_groups", ExistingItem())
                    hub = await session.get(GroupHub, gid)
                    if hub is None:
                        session.add(GroupHub(guild_id=gid, category_id=groups_cat))
                    elif not (item.bound_id and item.exists):
                        hub.category_id = groups_cat
                    bound.append("cat_groups")
                archive_cat = take("cat_group_archive")
                if archive_cat is not None:
                    item = existing.get("cat_group_archive", ExistingItem())
                    hub = await session.get(GroupHub, gid)
                    if hub is not None:
                        if not (hub.archive_category_id and item.exists):
                            hub.archive_category_id = archive_cat
                        bound.append("cat_group_archive")
                evidence = take("cat_evidence")
                archive = take("evidence_archive")
                mute = take("role_mute")
                mod = await session.get(ModSettings, gid)
                if mod is None and (evidence is not None or archive is not None or mute is not None):
                    mod = ModSettings(guild_id=gid)
                    session.add(mod)
                if mod is not None:
                    if evidence is not None:
                        item = existing.get("cat_evidence", ExistingItem())
                        if not (mod.evidence_category_id and item.exists):
                            mod.evidence_category_id = evidence
                        bound.append("cat_evidence")
                    if archive is not None:
                        item = existing.get("evidence_archive", ExistingItem())
                        if not (mod.evidence_archive_channel_id and item.exists):
                            mod.evidence_archive_channel_id = archive
                        bound.append("evidence_archive")
                    if mute is not None:
                        item = existing.get("role_mute", ExistingItem())
                        if not (mod.mute_role_id and item.exists):
                            mod.mute_role_id = mute
                        bound.append("role_mute")
                ladder = await session.get(LadderSettings, gid)
                if ladder is None:
                    ladder = LadderSettings(guild_id=gid)
                    session.add(ladder)
                for key, attr in _LADDER_ATTR.items():
                    role_id = take(key)
                    if role_id is None:
                        continue
                    item = existing.get(key, ExistingItem())
                    if not (getattr(ladder, attr) and item.exists):
                        setattr(ladder, attr, role_id)
                    bound.append(key)
                specs = default_tiers(LAYOUT.role_tiers)
                for key, index in _REP_INDEX.items():
                    role_id = take(key)
                    if role_id is None:
                        continue
                    row = await session.scalar(
                        select(ReputationTier).where(ReputationTier.guild_id == gid, ReputationTier.tier == index)
                    )
                    item = existing.get(key, ExistingItem())
                    if row is None:
                        spec = specs[index]
                        session.add(
                            ReputationTier(
                                guild_id=gid,
                                tier=index,
                                lower=spec.lower,
                                upper=spec.upper,
                                name=spec.name,
                                role_id=role_id,
                            )
                        )
                    elif not (row.role_id and item.exists):
                        row.role_id = role_id
                    bound.append(key)
                names = {
                    ("fixer", 1): LAYOUT.role_achievements[0],
                    ("fixer", 2): LAYOUT.role_achievements[1],
                    ("fixer", 3): LAYOUT.role_achievements[2],
                    ("ideologist", 1): LAYOUT.role_achievements[3],
                    ("ideologist", 2): LAYOUT.role_achievements[4],
                    ("ideologist", 3): LAYOUT.role_achievements[5],
                    ("vigilant", 1): LAYOUT.role_achievements[6],
                    ("vigilant", 2): LAYOUT.role_achievements[7],
                    ("vigilant", 3): LAYOUT.role_achievements[8],
                    ("vigilant", 4): LAYOUT.role_achievements[9],
                }
                for key, (ach_key, stage) in _ACH_KEYS.items():
                    role_id = take(key)
                    if role_id is None:
                        continue
                    row = await session.scalar(
                        select(AchievementDef).where(
                            AchievementDef.guild_id == gid,
                            AchievementDef.key == ach_key,
                            AchievementDef.stage == stage,
                        )
                    )
                    item = existing.get(key, ExistingItem())
                    thresholds = KEY_STAGES[ach_key]
                    threshold = thresholds[stage - 1]
                    if row is None:
                        session.add(
                            AchievementDef(
                                guild_id=gid,
                                key=ach_key,
                                stage=stage,
                                threshold=threshold,
                                role_id=role_id,
                                name=names[(ach_key, stage)],
                            )
                        )
                    elif not (row.role_id and item.exists):
                        row.role_id = role_id
                    bound.append(key)
        except Exception as exc:
            # Одна транзакция откатилась целиком — отчёт не должен говорить «привязано N».
            bound.clear()
            notes.append(copy.seed_note(copy.SEED_BIND, exc))
        return bound

    async def _side_effects(
        self,
        guild: discord.Guild,
        objects: dict[str, Any],
        *,
        actor_id: int,
        notes: list[str],
    ) -> None:
        await self._refresh(guild.id)
        caches = getattr(self._bot, "caches", None)
        invalidate = getattr(caches, "invalidate_access", None)
        if callable(invalidate):
            invalidate(guild.id)
        with suppress(Exception):
            await cached_snapshot(self._bot, guild.id)
        verify = getattr(self._bot, "verification", None)
        bind = getattr(verify, "bind", None)
        if callable(bind):
            try:
                await bind(guild.id)
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_VERIFY, exc))
        self_roles = getattr(self._bot, "self_roles", None)
        bind = getattr(self_roles, "bind", None)
        if callable(bind):
            try:
                await bind(guild.id)
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_SELFROLES, exc))
        rooms = getattr(self._bot, "rooms", None)
        preset = getattr(rooms, "configure_preset", None)
        if callable(preset):
            try:
                await preset(guild, preset=HubPreset.NAME)
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_HUB, exc))
        protect = getattr(getattr(self._bot, "loyalty", None), "protect_guild", None)
        if callable(protect) and any(key in objects for key in _OLD_KEYS):
            try:
                await protect(guild)
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_OLD, exc))
        mute = objects.get("role_mute")
        setter = getattr(getattr(self._bot, "moderation", None), "set_mute_role", None)
        if callable(setter) and mute is not None:
            try:
                await setter(guild.id, mute.id)
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_MUTE, exc))
        ladder = getattr(self._bot, "ladder", None)
        update = getattr(ladder, "update_config", None)
        if callable(update) and all(key in objects for key in _LADDER_ATTR):
            try:
                await update(
                    guild.id,
                    guest_role_id=objects["role_guest"].id,
                    regular_role_id=objects["role_regular"].id,
                    resident_role_id=objects["role_resident"].id,
                    friend_role_id=objects["role_friend"].id,
                )
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_LADDER, exc))
        reputation = getattr(self._bot, "reputation", None)
        setter = getattr(reputation, "set_tier_role", None)
        if callable(setter):
            for key, index in _REP_INDEX.items():
                role = objects.get(key)
                if role is None:
                    continue
                try:
                    await setter(guild.id, index, role.id)
                except Exception as exc:
                    notes.append(copy.seed_note(copy.SEED_REP, exc))
                    break
        achievements = getattr(self._bot, "achievements", None)
        setter = getattr(achievements, "set_role", None)
        if callable(setter):
            for key, (ach_key, stage) in _ACH_KEYS.items():
                role = objects.get(key)
                if role is None:
                    continue
                try:
                    await setter(guild.id, ach_key, stage, role.id)
                except Exception as exc:
                    notes.append(copy.seed_note(copy.SEED_ACHIEVE, exc))
                    break
        lfg = getattr(self._bot, "lfg", None)
        setter = getattr(lfg, "set_channel", None)
        channel = objects.get("lfg")
        if callable(setter) and channel is not None:
            try:
                await setter(guild.id, int(channel.id))
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_LFG, exc))
        poll = getattr(self._bot, "poll", None)
        setter = getattr(poll, "set_channel", None)
        channel = objects.get("polls")
        if callable(setter) and channel is not None:
            try:
                await setter(guild.id, int(channel.id))
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_POLLS, exc))
        ping = getattr(poll, "set_ping_role", None)
        role = objects.get("role_sub_polls")
        if callable(ping) and role is not None:
            try:
                await ping(guild.id, int(role.id))
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_POLLS, exc))
        stats = getattr(self._bot, "stats_board", None)
        setter = getattr(stats, "set_channel", None)
        channel = objects.get("stats")
        if callable(setter) and channel is not None:
            try:
                await setter(guild.id, int(channel.id))
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_STATS, exc))
        del actor_id

    async def _panels(
        self,
        guild: discord.Guild,
        objects: dict[str, Any],
        *,
        actor_id: int,
        notes: list[str],
        only_keys: frozenset[str] | None,
    ) -> None:
        showcase = getattr(self._bot, "showcase", None)
        poster = getattr(showcase, "post", None)
        if not callable(poster):
            return

        def wanted(*keys: str) -> bool:
            return only_keys is None or any(key in only_keys for key in keys)

        states_fn = getattr(showcase, "statuses", None)
        states: dict[str, object] = {}
        invalidate = getattr(showcase, "invalidate", None)
        if callable(invalidate):
            # Кэш статусов мог быть снят до того, как этот прогон пересоздал каналы.
            invalidate(int(guild.id))
        if callable(states_fn):
            try:
                states = dict(await states_fn(guild))
            except Exception:
                log.exception("seed_showcase_status_failed", guild_id=int(guild.id))
        for spec in SHOWCASES:
            keys = (spec.channel_key, *spec.extra_keys)
            if not wanted(*keys):
                continue
            channel = objects.get(spec.channel_key)
            if channel is None:
                continue
            # Живую витрину в привязанном канале повторная сборка не перевыпускает.
            if states.get(spec.id) is ShowcaseState.LIVE:
                continue
            try:
                result = await poster(guild, spec, channel_id=int(channel.id), actor_id=actor_id)
            except Exception as exc:
                notes.append(copy.seed_note(spec.label, exc))
                continue
            if not getattr(result, "ok", False):
                notes.append(getattr(result, "error", None) or copy.seed_note(spec.label, copy.STALE_BODY))

    async def _data(self, guild: discord.Guild, objects: dict[str, Any], notes: list[str]) -> None:
        ensure = getattr(getattr(self._bot, "self_roles", None), "ensure_defaults", None)
        if callable(ensure):
            try:
                await ensure(guild.id)
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_DATA, exc))
        del objects

    async def _census(
        self,
        guild: discord.Guild,
        objects: dict[str, Any],
        notes: list[str],
    ) -> tuple[bool, int]:
        special_granted = False
        special = objects.get("role_special")
        owner_id = getattr(guild, "owner_id", None)
        if special is not None and owner_id:
            held = False
            for member in getattr(guild, "members", ()) or ():
                if any(int(getattr(role, "id", 0)) == int(special.id) for role in getattr(member, "roles", ()) or ()):
                    held = True
                    break
            if not held:
                try:
                    result = await self._bot.roles.apply(
                        RoleClaim(
                            guild_id=guild.id,
                            user_id=int(owner_id),
                            subsystem=RoleSubsystem.OWNER,
                            add=(int(special.id),),
                            reason=SEED_REASON,
                            urgent=True,
                            actor_id=int(owner_id),
                        )
                    )
                    special_granted = bool(getattr(result, "ok", False))
                    if not special_granted:
                        notes.append(
                            copy.seed_note(copy.SEED_SPECIAL, getattr(result, "error", None) or copy.SEED_NOT_GRANTED)
                        )
                except Exception as exc:
                    notes.append(copy.seed_note(copy.SEED_SPECIAL, exc))
        await self._refresh(guild.id)
        humans = 0
        run = getattr(self._bot, "run_census", None)
        if callable(run):
            try:
                result = await run(guild)
                humans = int(getattr(result, "humans", 0) or 0)
            except Exception as exc:
                notes.append(copy.seed_note(copy.SEED_CENSUS, exc))
        else:
            notes.append(copy.SEED_CENSUS_DOWN)
        guest = objects.get("role_guest")
        if guest is not None:
            ladder_ids = [
                objects[key].id
                for key in ("role_guest", "role_regular", "role_resident", "role_friend")
                if key in objects
            ]
            snapshot = [
                (
                    int(member.id),
                    bool(getattr(member, "bot", False)),
                    frozenset(int(role.id) for role in getattr(member, "roles", ()) or ()),
                )
                for member in getattr(guild, "members", ()) or ()
            ]
            for user_id in seed_guest_plan(members=snapshot, guest_role_id=int(guest.id), ladder_role_ids=ladder_ids):
                try:
                    result = await self._bot.roles.apply(
                        RoleClaim(
                            guild_id=guild.id,
                            user_id=user_id,
                            subsystem=RoleSubsystem.ACTIVITY,
                            add=(int(guest.id),),
                            reason=SEED_REASON,
                        )
                    )
                    if not getattr(result, "ok", False):
                        notes.append(
                            copy.seed_grant(
                                copy.SEED_GUEST, user_id, getattr(result, "error", None) or copy.SEED_NOT_GRANTED
                            )
                        )
                except Exception as exc:
                    notes.append(copy.seed_grant(copy.SEED_GUEST, user_id, exc))
        reliable = objects.get("role_reliable")
        if reliable is not None:
            async with session_scope(self._bot.sessions) as session:
                have = set(
                    int(user_id)
                    for user_id in (
                        await session.scalars(select(Reputation.user_id).where(Reputation.guild_id == guild.id))
                    ).all()
                )
            for member in getattr(guild, "members", ()) or ():
                if getattr(member, "bot", False) or int(member.id) in have:
                    continue
                if any(int(getattr(role, "id", 0)) == int(reliable.id) for role in getattr(member, "roles", ()) or ()):
                    continue
                try:
                    result = await self._bot.roles.apply(
                        RoleClaim(
                            guild_id=guild.id,
                            user_id=int(member.id),
                            subsystem=RoleSubsystem.REPUTATION,
                            add=(int(reliable.id),),
                            reason=SEED_REASON,
                        )
                    )
                    if not getattr(result, "ok", False):
                        notes.append(
                            copy.seed_grant(
                                copy.SEED_RELIABLE,
                                member.id,
                                getattr(result, "error", None) or copy.SEED_NOT_GRANTED,
                            )
                        )
                except Exception as exc:
                    notes.append(copy.seed_grant(copy.SEED_RELIABLE, member.id, exc))
        return special_granted, humans

    async def _refresh(self, guild_id: int) -> None:
        refresh = getattr(self._bot, "refresh_config", None)
        if callable(refresh):
            try:
                await refresh(int(guild_id))
            except Exception:
                log.exception("seed_refresh_failed", guild_id=int(guild_id))

    async def _refresh_guild(self, guild: discord.Guild) -> None:
        fetch = getattr(guild, "fetch_channels", None)
        if callable(fetch):
            with suppress(discord.HTTPException, discord.RateLimited):
                await self._discord(fetch())
        me = getattr(guild, "me", None)
        getter = getattr(guild, "fetch_member", None)
        if callable(getter) and me is not None:
            with suppress(discord.HTTPException, discord.RateLimited):
                await self._discord(getter(int(me.id)))

    async def _discord(self, awaitable: Any) -> Any:
        if _build_session.get() is None:
            return await self._send_discord(awaitable)
        # HTTPClient.__aexit__ иногда ждёт reset даже после отмены запроса.
        # Команда не ждёт сетевую очистку; отменённый запрос новых попыток не делает.
        task = asyncio.create_task(self._send_discord(awaitable))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            self._cancelled_http.add(task)
            task.add_done_callback(self._http_cancelled)
            raise

    def _http_cancelled(self, task: asyncio.Task[Any]) -> None:
        self._cancelled_http.discard(task)
        if not task.cancelled():
            task.exception()

    async def _send_discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


MappingIds = dict[str, int | None]
SKELETON_KEYS = skeleton_entries(with_old=True)


def _object_for(
    guild: discord.Guild,
    key: str,
    objects: dict[str, Any],
    existing: dict[str, ExistingItem],
) -> Any:
    if not key:
        return None
    if key in objects:
        return objects[key]
    entry = entry_by_key(key)
    if entry is None:
        return None
    return _resolve_object(guild, entry, existing.get(key, ExistingItem())) or _named(guild, entry)


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _count(
    entry: SkeletonEntry,
    action: str,
    created: int,
    reused: int,
    repaired: int,
    created_roles: int,
    reused_roles: int,
    created_channels: int,
    reused_channels: int,
    created_categories: int,
) -> tuple[int, int, int, int, int, int, int, int]:
    if action == "create":
        created += 1
        if entry.kind is SkeletonKind.ROLE:
            created_roles += 1
        elif entry.kind is SkeletonKind.CATEGORY:
            created_categories += 1
        else:
            created_channels += 1
    elif action == "repair":
        repaired += 1
        if entry.kind is SkeletonKind.ROLE:
            reused_roles += 1
        else:
            reused_channels += 1
    elif action in {"reuse", "by_name"}:
        reused += 1
        if entry.kind is SkeletonKind.ROLE:
            reused_roles += 1
        else:
            reused_channels += 1
    return created, reused, repaired, created_roles, reused_roles, created_channels, reused_channels, created_categories


def _bot_top_position(guild: discord.Guild) -> int | None:
    me = getattr(guild, "me", None)
    if me is None:
        return None
    mine = {int(getattr(role, "id", 0) or 0) for role in getattr(me, "roles", ()) or ()}
    scanned = [
        int(getattr(role, "position", 0) or 0)
        for role in getattr(guild, "roles", ()) or ()
        if int(getattr(role, "id", 0) or 0) in mine
    ]
    if scanned:
        return max(scanned)
    top = getattr(me, "top_role", None)
    pos = getattr(top, "position", None)
    return None if pos is None else int(pos)


def _blocking_positions(guild: discord.Guild) -> tuple[int, ...]:
    names = {entry.name.casefold() for entry in skeleton_entries(with_old=True)}
    for entry in skeleton_entries(with_old=True):
        names.update(alias.casefold() for alias in entry.aliases)
    found: list[int] = []
    for role in getattr(guild, "roles", ()) or ():
        if str(getattr(role, "name", "") or "").casefold() not in names:
            continue
        position = getattr(role, "position", None)
        if position is not None:
            found.append(int(position))
    return tuple(found)


def _align_targets(
    guild: discord.Guild,
    objects: dict[str, Any],
    existing: dict[str, ExistingItem],
    wanted: SeedPlan,
    *,
    with_old: bool,
) -> dict[str, Any]:
    role_keys = [key for key in objects if (entry := entry_by_key(key)) is not None and entry.kind is SkeletonKind.ROLE]
    if not role_keys:
        return {}
    # Созданная роль встаёт в самый низ списка, а её position_ok считался до создания.
    need = any(
        key in wanted.by_name or key in wanted.create or not existing.get(key, ExistingItem()).position_ok
        for key in role_keys
    )
    if not need:
        return {}
    found = dict(objects)
    for key in role_order(with_old=with_old):
        if key in found:
            continue
        entry = entry_by_key(key)
        if entry is None:
            continue
        obj = _resolve_object(guild, entry, existing.get(key, ExistingItem())) or _named(guild, entry)
        if obj is not None:
            found[key] = obj
    return found


def _lookup(guild: discord.Guild, entry: SkeletonEntry | None, bound_id: int | None) -> Any:
    if entry is None or bound_id is None:
        return None
    if entry.kind is SkeletonKind.ROLE:
        getter = getattr(guild, "get_role", None)
        if callable(getter):
            return getter(int(bound_id))
        return next((role for role in getattr(guild, "roles", ()) or () if int(role.id) == int(bound_id)), None)
    getter = getattr(guild, "get_channel", None)
    if callable(getter):
        return getter(int(bound_id))
    return None


def _named(guild: discord.Guild, entry: SkeletonEntry | None) -> Any:
    if entry is None:
        return None
    if entry.kind is SkeletonKind.ROLE:
        pool: Iterable[Any] = getattr(guild, "roles", ()) or ()
    elif entry.kind is SkeletonKind.CATEGORY:
        pool = getattr(guild, "categories", ()) or ()
    elif entry.kind is SkeletonKind.VOICE:
        pool = getattr(guild, "voice_channels", ()) or ()
    else:
        pool = getattr(guild, "text_channels", ()) or ()
    names = (entry.name, *entry.aliases)
    for obj in pool:
        title = str(getattr(obj, "name", "") or "")
        if any(is_same_name(title, name) for name in names):
            return obj
    return None


def _resolve_object(guild: discord.Guild, entry: SkeletonEntry, item: ExistingItem) -> Any:
    if item.bound_id and item.exists:
        return _lookup(guild, entry, item.bound_id)
    if item.found_by_name:
        return _lookup(guild, entry, item.found_id) or _named(guild, entry)
    return None


def _gate_targets(guild: discord.Guild, entry: SkeletonEntry, ids: MappingIds) -> tuple[Any, ...]:
    if entry.gate is SkeletonGate.VERIFIED:
        keys: tuple[str, ...] = ("role_verified", *_RANK_KEYS)
    elif entry.gate is SkeletonGate.STAFF:
        keys = tuple(_RANK_KEYS)
    elif entry.gate is SkeletonGate.EVIDENCE or entry.gate is SkeletonGate.GROUP_ARCHIVE:
        keys = ("role_special", "role_chief")
    else:
        return ()
    found: list[Any] = []
    for key in keys:
        role = _lookup(guild, entry_by_key(key), _as_int(ids.get(key)))
        if role is not None:
            found.append(role)
    return tuple(found)


def _right_actors(
    guild: discord.Guild,
    *,
    objects: dict[str, Any] | None = None,
    existing: dict[str, ExistingItem] | None = None,
    ids: MappingIds | None = None,
) -> dict[str, tuple[Any, ...]]:
    def resolve(key: str) -> Any:
        if objects is not None:
            return _object_for(guild, key, objects, existing or {})
        return _lookup(guild, entry_by_key(key), _as_int((ids or {}).get(key)))

    found: dict[str, tuple[Any, ...]] = {}
    everyone = getattr(guild, "default_role", None)
    if everyone is not None:
        found["everyone"] = (everyone,)
    verified = resolve("role_verified")
    if verified is not None:
        found["verified"] = (verified,)
    staff = tuple(role for key in _RANK_KEYS if (role := resolve(key)) is not None)
    if staff:
        found["staff"] = staff
    chief = tuple(role for key in ("role_special", "role_chief") if (role := resolve(key)) is not None)
    if chief:
        found["chief"] = chief
    mute = resolve("role_mute")
    if mute is not None:
        found["mute"] = (mute,)
    me = getattr(guild, "me", None)
    if me is not None:
        found["bot"] = (me,)
    return found


def _actual_rights(channel: Any, actors: Mapping[str, tuple[Any, ...]]) -> dict[str, dict[str, bool | None]]:
    actual: dict[str, dict[str, bool | None]] = {}
    for name, targets in actors.items():
        bags = [_flags_of(_overwrite_for(channel, target)) for target in targets]
        actual[name] = _collapse_flags(bags)
    return actual


def _flags_of(overwrite: Any) -> dict[str, bool | None]:
    return {name: _flag(overwrite, name) for name in PERMISSIONS}


def _collapse_flags(bags: list[dict[str, bool | None]]) -> dict[str, bool | None]:
    if not bags:
        return {}
    merged: dict[str, bool | None] = {}
    for name in PERMISSIONS:
        values = [bag.get(name) for bag in bags]
        first = values[0]
        merged[name] = first if all(value is first for value in values) else None
    return merged


def _merged_overwrite(current: Any, needed: Mapping[str, bool]) -> discord.PermissionOverwrite:
    values: dict[str, bool] = {}
    if current is not None:
        for name in PERMISSIONS:
            value = _flag(current, name)
            if value is not None:
                values[name] = value
    values.update(needed)
    return discord.PermissionOverwrite(**values)


def _bot_role_below(guild: discord.Guild, target: Any) -> bool:
    me = getattr(guild, "me", None)
    top = getattr(me, "top_role", None) if me is not None else None
    top_pos = getattr(top, "position", None)
    position = getattr(target, "position", None)
    if top_pos is None or position is None:
        return False
    return int(position) >= int(top_pos)


def _overwrites_match(guild: discord.Guild, obj: Any, entry: SkeletonEntry, ids: MappingIds) -> bool:
    return not missing_rules(entry, _actual_rights(obj, _right_actors(guild, ids=ids)))


def _overwrite_for(channel: Any, target: Any) -> Any:
    raw = getattr(channel, "overwrites", {}) or {}
    try:
        if target in raw:
            return raw[target]
    except TypeError:
        pass
    tid = getattr(target, "id", None)
    if tid in raw:
        return raw[tid]
    for key, value in raw.items():
        if getattr(key, "id", None) == tid:
            return value
    return None


def _flag(overwrite: Any, attr: str) -> bool | None:
    if overwrite is None:
        return None
    return getattr(overwrite, attr, None)


def _role_positions_ok(guild: discord.Guild, ids: MappingIds, *, with_old: bool) -> dict[str, bool]:
    order = role_order(with_old=with_old)
    present: list[tuple[str, int]] = []
    for key in order:
        role = _lookup(guild, entry_by_key(key), _as_int(ids.get(key)))
        if role is None:
            continue
        present.append((key, int(getattr(role, "position", 0) or 0)))
    ok = {key: True for key in order}
    for index in range(len(present) - 1):
        if present[index][1] <= present[index + 1][1]:
            ok[present[index][0]] = False
            ok[present[index + 1][0]] = False
    return ok


async def _upsert_layout(session: Any, guild_id: int, key: str, channel_id: int) -> None:
    row = await session.scalar(select(GuildLayout).where(GuildLayout.guild_id == guild_id, GuildLayout.key == key))
    if row is None:
        session.add(GuildLayout(guild_id=guild_id, key=key, channel_id=int(channel_id)))
        return
    row.channel_id = int(channel_id)
