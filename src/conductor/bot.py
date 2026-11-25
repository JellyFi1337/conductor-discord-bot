from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections import deque
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import discord
import structlog
from discord import app_commands
from discord.app_commands import Command, Group
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from conductor import __version__
from conductor.config import Settings
from conductor.db.engine import create_engine, create_sessionmaker, init_db, require_schema_head, script_head
from conductor.db.models import GuildConfig
from conductor.db.session import session_scope
from conductor.discord_compat import install_ratelimit_compat
from conductor.domain.guilds import BotMode
from conductor.domain.voice import LiveVoice
from conductor.presentation import copy
from conductor.presentation.icons import bind as bind_icons
from conductor.presentation.interact import STALE_INTERACTION
from conductor.presentation.layout import command_failure, error_code, respond
from conductor.presentation.motion import EmojiRegistry
from conductor.presentation.persistent import load_persistent_ui
from conductor.services import heartbeat
from conductor.services.access import bind_access_jobs, cached_decide, ensure_defaults, sweep_orphaned_special
from conductor.services.achievements import AchievementService
from conductor.services.activity import ActivityCollector
from conductor.services.automod import AutomodService
from conductor.services.automod_rules import AutomodRules
from conductor.services.blueprint import BlueprintService
from conductor.services.bugs import BugService
from conductor.services.cache import CacheHub, CacheMapping, roles_changed
from conductor.services.card_refresh import CardRefresher
from conductor.services.census import CensusResult, MemberSnapshot, apply_census
from conductor.services.create_lock import hydrate
from conductor.services.domain_events import DomainEvents
from conductor.services.dossier import DossierService
from conductor.services.errors import CriticalLog
from conductor.services.evidence import EvidenceService
from conductor.services.gathering import GatheringService
from conductor.services.giveaway import GiveawayService
from conductor.services.groups import GroupService
from conductor.services.guild_config import get_or_create_config, remember_tz
from conductor.services.history import HistoryLedger
from conductor.services.invites import InviteTracker, UsedInvite
from conductor.services.ladder import LadderService
from conductor.services.lfg import LfgService
from conductor.services.limits import InteractionLimiter, OutboundGate
from conductor.services.log_pump import LogPump
from conductor.services.loyalty import LoyaltyService
from conductor.services.message_cache import MessageCache
from conductor.services.moderation import ModerationService
from conductor.services.mutation_lock import DiscordMutationLock
from conductor.services.observe import ObserveService
from conductor.services.old_roles import old_role_id_set
from conductor.services.ops import OwnerOps
from conductor.services.panels import probe_tick, restore_panels
from conductor.services.poll import PollService
from conductor.services.privacy import PrivacyService
from conductor.services.pulse import PulseService
from conductor.services.queue import ActionQueue
from conductor.services.rate_observer import RateObserver
from conductor.services.recruit import RecruitService
from conductor.services.reminders import ReminderService
from conductor.services.renames import RenameService
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleBroker, RoleSubsystem
from conductor.services.runtime import log_tick
from conductor.services.scheduler import Scheduler
from conductor.services.seed import GuildSeed
from conductor.services.self_roles import SelfRolesService
from conductor.services.setup_map import SetupMapper
from conductor.services.showcase import ShowcaseService
from conductor.services.staff_feed import StaffFeed
from conductor.services.stats_board import StatsBoardService
from conductor.services.structure import StructureWatch
from conductor.services.tickets import TicketService
from conductor.services.traffic import TrafficGovernor
from conductor.services.verification import VerificationService
from conductor.services.voice_rooms import VoiceRoomService
from conductor.services.voice_sessions import VoiceSessionTracker
from conductor.services.watchers import WatchersService
from conductor.services.watchers_report import WatchersReportService
from conductor.services.youtube import YoutubeService

log = structlog.get_logger("conductor")

COGS = (
    "conductor.cogs.admin",
    "conductor.cogs.diagnostics",
    "conductor.cogs.guide",
    "conductor.cogs.telemetry",
    "conductor.cogs.stats",
    "conductor.cogs.user_logs",
    "conductor.cogs.admin_logs",
    "conductor.cogs.loyalty",
    "conductor.cogs.watchers",
    "conductor.cogs.voice",
    "conductor.cogs.tickets",
    "conductor.cogs.moderation",
    "conductor.cogs.automod",
    "conductor.cogs.verification",
    "conductor.cogs.self_roles",
    "conductor.cogs.dossier",
    "conductor.cogs.profile",
    "conductor.cogs.giveaway",
    "conductor.cogs.poll",
    "conductor.cogs.events",
    "conductor.cogs.groups",
    "conductor.cogs.reminders",
    "conductor.cogs.bugs",
    "conductor.cogs.recruit",
    "conductor.cogs.skeleton",
    "conductor.cogs.ops",
)

COMMAND_HASH_FILE = ".command-hash"


def command_hash_path(settings: Settings) -> Path:
    return settings.data_dir / COMMAND_HASH_FILE


def snapshot_tree(
    tree: app_commands.CommandTree,
    *,
    guild: discord.abc.Snowflake | None = None,
) -> list[dict[str, Any]]:
    rows = [_snapshot_command(command) for command in tree.walk_commands(guild=guild)]
    rows.sort(key=lambda row: (row["path"], row["kind"]))
    return rows


def tree_fingerprint(rows: Sequence[Mapping[str, Any]], *, scope: int | str | None) -> str:
    payload = {"scope": "global" if scope is None else str(scope), "commands": list(rows)}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def read_command_hash(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    token = raw.split()[0] if raw else ""
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        return None
    return token


def write_command_hash(path: Path, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(digest + "\n", encoding="utf-8")


def _enum_int(value: object) -> int:
    """Python 3.13 отдаёт enum Discord как `_EnumValue_*`, не как int."""
    current: object = value
    for _ in range(4):
        if isinstance(current, int) and not isinstance(current, bool):
            return current
        nxt = getattr(current, "value", None)
        if nxt is None or nxt is current:
            break
        current = nxt
    return int(current)  # type: ignore[arg-type]


def _snapshot_command(command: Command[Any, ..., Any] | Group) -> dict[str, Any]:
    default = command.default_permissions
    params: list[dict[str, Any]] = []
    if isinstance(command, Command):
        for param in command.parameters:
            params.append(
                {
                    "name": param.name,
                    "description": param.description,
                    "required": param.required,
                    "type": _enum_int(param.type),
                    "choices": [[choice.name, choice.value] for choice in param.choices],
                }
            )
    return {
        "path": command.qualified_name,
        "kind": "group" if isinstance(command, Group) else "command",
        "description": command.description,
        "nsfw": bool(command.nsfw),
        "guild_only": bool(getattr(command, "guild_only", False)),
        "default_permissions": None if default is None else int(default.value),
        "params": params,
    }


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = True
    intents.message_content = True
    intents.moderation = True
    intents.voice_states = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.guild_reactions = True
    intents.invites = True
    intents.auto_moderation = True
    intents.presences = False
    return intents


def live_voices(guild: discord.Guild) -> tuple[LiveVoice, ...]:
    rows: list[LiveVoice] = []
    for channel in (*guild.voice_channels, *guild.stage_channels):
        for member in channel.members:
            if getattr(member, "bot", False):
                continue  # боты в голосе — не активность участников (см. Telemetry.on_voice_state_update)
            rows.append(LiveVoice(guild_id=guild.id, user_id=member.id, channel_id=channel.id))
    return tuple(rows)


_PASSTHROUGH_EVENTS = frozenset(
    {
        "ready",
        "connect",
        "disconnect",
        "resumed",
        "shard_ready",
        "shard_connect",
        "shard_disconnect",
        "shard_resumed",
        "guild_join",
        "guild_remove",
        "error",
        "socket_event_type",
        "socket_raw_receive",
        "socket_raw_send",
    }
)

def event_guild_id(*args: object) -> int | None:
    for arg in args:
        if arg is None:
            continue
        guild_id = getattr(arg, "guild_id", None)
        if isinstance(guild_id, int):
            return guild_id
        guild = getattr(arg, "guild", None)
        if guild is not None:
            found = getattr(guild, "id", None)
            if isinstance(found, int):
                return found
        if type(arg).__name__ == "Guild":
            found = getattr(arg, "id", None)
            if isinstance(found, int):
                return found
    return None


def apply_application_owners(bot: Any, info: object) -> None:
    """Выставить owner_id / owner_ids так, как это делает discord.py is_owner."""
    team = getattr(info, "team", None)
    if team is not None:
        allowed = (discord.TeamMemberRole.admin, discord.TeamMemberRole.developer)
        bot.owner_id = None
        bot.owner_ids = {
            member.id
            for member in getattr(team, "members", ())
            if getattr(member, "role", None) in allowed
        }
        return
    owner = getattr(info, "owner", None)
    owner_id = getattr(owner, "id", None)
    bot.owner_id = int(owner_id) if owner_id is not None else None


class Conductor(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        install_ratelimit_compat()
        self.settings = settings
        observer = RateObserver()
        self.rate_observer = observer
        self.traffic = TrafficGovernor(self)
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(),
            chunk_guilds_at_startup=True,
            max_messages=settings.message_cache_size,
            help_command=None,
            http_trace=self.traffic.trace_config(),
            # Адаптер применяет порог к ожиданию, только когда жетоны исчерпаны.
            max_ratelimit_timeout=self.traffic.limits.max_ratelimit_wait,
        )
        self.engine: AsyncEngine = create_engine(settings.database_url)
        self.sessions: async_sessionmaker[AsyncSession] = create_sessionmaker(self.engine)
        self.collector = ActivityCollector()
        self.history = HistoryLedger()
        self.history_busy: set[int] = set()
        self.invites = InviteTracker()
        self.voice_sessions = VoiceSessionTracker(self.collector)
        self.last_used_invite: dict[int, UsedInvite | None] = {}
        self.caches = CacheHub()
        self.config_by_guild: CacheMapping[int, GuildConfig] = CacheMapping(self.caches.guild_config)
        self.messages = MessageCache(
            maxlen=settings.message_cache_size,
            ttl=float(settings.message_cache_ttl),
        )
        self.outbound = OutboundGate(governor=self.traffic)
        limits = self.traffic.limits
        env_clicks = settings.interaction_limit_per_minute
        guild_clicks = limits.click_guild_limit if env_clicks == 50 else env_clicks
        self.interactions = InteractionLimiter(
            limit=guild_clicks,
            member_limit=limits.click_member_limit,
            member_window=limits.click_member_window,
        )
        self.scheduler = Scheduler(self.spawn)
        self.queue = ActionQueue(self.spawn)
        self.cards = CardRefresher(self)
        self.rooms = VoiceRoomService(self)
        self.tickets = TicketService(self)
        self.moderation = ModerationService(self)
        self.automod_rules = AutomodRules(self)
        self.automod = AutomodService(self)
        self.verification = VerificationService(self)
        self.self_roles = SelfRolesService(self)
        self.stats_board = StatsBoardService(self)
        self.dossier = DossierService(self)
        self.evidence = EvidenceService(self)
        self.events = DomainEvents()
        self.reputation = ReputationService(self)
        self.achievements = AchievementService(self)
        self.reminders = ReminderService(self)
        self.ladder = LadderService(self)
        self.giveaways = GiveawayService(self)
        self.polls = PollService(self)
        self.gatherings = GatheringService(self)
        self.groups = GroupService(self)
        self.bugs = BugService(self)
        self.loyalty = LoyaltyService(self)
        self.watchers = WatchersService(self)
        self.watchers_report = WatchersReportService(self)
        self.youtube = YoutubeService(self)
        self.lfg = LfgService(self)
        self.http_session: aiohttp.ClientSession | None = None
        self.roles = RoleBroker(self, interval=self.traffic.limits.role_coalesce)
        self.log_pump = LogPump(self)
        self.staff_feed = StaffFeed(self)
        self.errors = CriticalLog(self)
        self.observe = ObserveService(self)
        self.pulse = PulseService(self)
        self.recruit = RecruitService(self)
        self.privacy = PrivacyService(self)
        self.renames = RenameService(self)
        self.ops = OwnerOps(self)
        self.seed = GuildSeed(self)
        self.setup_map = SetupMapper(self)
        self.structure = StructureWatch(self)
        self.showcase = ShowcaseService(self)
        self.blueprint = BlueprintService(self)
        self.fence = settings.fence
        self._mutation_lock = DiscordMutationLock()
        self.motion = EmojiRegistry()
        self.attach_icons()
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._booted = False
        self._startup_ready = False
        self._boot_started = time.perf_counter()
        self._resume_at: deque[float] = deque()
        self._closing = False
        self.startup_ms: dict[str, int] = {}
        self._panel_probe_after = 0
        self.scheduler.every(60, "panels-probe", lambda: probe_tick(self))
        self.scheduler.every(3600, "groups-sweep", self.groups.sweep_hourly)
        self.scheduler.every(heartbeat.PERIOD_SECONDS, "heartbeat", self._touch_heartbeat)
        bind_access_jobs(self)

    async def _touch_heartbeat(self) -> None:
        ok = heartbeat.touch(heartbeat.heartbeat_path(self.settings))
        log_tick(log, "heartbeat", changed=0, ok=ok)

    def resumes_last_hour(self) -> int:
        now = time.monotonic()
        cutoff = now - 3600.0
        while self._resume_at and self._resume_at[0] < cutoff:
            self._resume_at.popleft()
        return len(self._resume_at)

    def serves(self, guild_id: int | None) -> bool:
        if guild_id is None:
            return False
        if self.fence is None:
            return True
        return self.fence.serves(guild_id)

    def may_reside(self, guild_id: int) -> bool:
        if self.fence is None:
            return True
        return self.fence.may_reside(guild_id)

    def is_developer(self, user_id: int) -> bool:
        if user_id in self.settings.owner_user_ids:
            return True
        if self.owner_id is not None and user_id == self.owner_id:
            return True
        ids = self.owner_ids
        return bool(ids) and user_id in ids

    def attach_icons(self) -> None:
        bind_icons(resolve=self.motion.resolve, button=self.motion.partial)

    def dispatch(self, event_name: str, /, *args: Any, **kwargs: Any) -> None:
        if event_name not in _PASSTHROUGH_EVENTS:
            guild_id = event_guild_id(*args)
            if guild_id is not None and not self.may_reside(guild_id):
                return
            if guild_id is not None and not self.serves(guild_id):
                if event_name == "interaction" and args:
                    self.spawn(self._ack_ignored(args[0]), name="ignore-guild-interaction")
                return
        super().dispatch(event_name, *args, **kwargs)

    async def _ack_ignored(self, interaction: object) -> None:
        response = getattr(interaction, "response", None)
        send = getattr(response, "send_message", None)
        done = getattr(response, "is_done", None)
        if not callable(send) or (callable(done) and done()):
            return
        try:
            await send(copy.GUILD_IGNORED, ephemeral=True)
        except discord.HTTPException:
            return

    async def enforce_guild_fence(self) -> None:
        for guild in tuple(self.guilds):
            if self.may_reside(guild.id):
                continue
            log.warning("leaving_foreign_guild", guild_id=guild.id, name=guild.name)
            try:
                await guild.leave()
            except discord.HTTPException:
                log.exception("guild_leave_failed", guild_id=guild.id)

    def _mark_startup(self, stage: str, started: float) -> None:
        ms = int((time.perf_counter() - started) * 1000)
        self.startup_ms[stage] = ms
        log.debug("startup_ms", stage=stage, ms=ms)

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        patched = self._mutation_lock.install()
        log.info("role_broker_gate", patched=patched, mode=self.settings.mode_label)
        hydrate(self)
        started = time.perf_counter()
        try:
            info = await self.application_info()
        except Exception:
            log.exception("application_info_failed")
        else:
            apply_application_owners(self, info)
        self._mark_startup("application_info", started)
        started = time.perf_counter()
        await init_db(self.engine, auto_migrate=self.settings.auto_migrate_enabled)
        if not self.settings.auto_migrate_enabled:
            await require_schema_head(self.engine)
        try:
            loaded = self.automod_rules.load()
            log.info(
                "automod_rules_loaded",
                missing=loaded.missing,
                errors=len(loaded.errors),
                path=str(self.automod_rules.path),
            )
        except Exception:
            log.exception("automod_rules_load_failed")
        self._mark_startup("migrations", started)
        await self.traffic.hydrate()
        self.tree.error(self._on_app_command_error)
        started = time.perf_counter()
        for extension in COGS:
            await self.load_extension(extension)
            log.info("cog_loaded", extension=extension)
        items = load_persistent_ui()
        if items:
            self.add_dynamic_items(*items)
            log.info("dynamic_items_registered", count=len(items))
        self._mark_startup("cogs", started)
        started = time.perf_counter()
        try:
            await self.motion.refresh(self)
            self.attach_icons()
        except Exception:
            log.exception("motion_emoji_refresh_failed")
        self._mark_startup("motion", started)
        started = time.perf_counter()
        try:
            report = await restore_panels(self)
            log.info("panels_restored", live=report.live, stale=report.stale)
        except Exception:
            log.exception("panels_restore_failed")
        self._mark_startup("panels", started)
        self.scheduler.start()
        self.queue.start()
        started = time.perf_counter()
        try:
            restored = await self.giveaways.restore()
            log.info("giveaways_restored", count=restored)
        except Exception:
            log.exception("giveaway_restore_failed")
        try:
            restored = await self.polls.restore()
            log.info("polls_restored", count=restored)
        except Exception:
            log.exception("poll_restore_failed")
        try:
            restored = await self.gatherings.restore()
            log.info("gatherings_restored", count=restored)
        except Exception:
            log.exception("gathering_restore_failed")
        try:
            restored = await self.groups.restore()
            log.info("groups_restored", count=restored)
        except Exception:
            log.exception("group_restore_failed")
        try:
            restored = await self.reminders.restore()
            log.info("reminders_restored", count=restored)
        except Exception:
            log.exception("reminder_restore_failed")
        try:
            restored = await self.ladder.restore()
            log.info("ladder_restored", count=restored)
        except Exception:
            log.exception("ladder_restore_failed")
        self._mark_startup("restores", started)
        started = time.perf_counter()
        await self._sync_application_commands()
        self._mark_startup("command_sync", started)

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure) and interaction.response.is_done():
            return
        original = getattr(error, "original", None)
        if isinstance(original, discord.HTTPException) and original.code in STALE_INTERACTION:
            log.warning("interaction_expired", code=original.code)
            return
        reporter = getattr(self, "errors", None)
        report = getattr(reporter, "report_interaction", None)
        if callable(report):
            try:
                await report(interaction, error)
            except Exception:
                log.exception("critical_report_failed", error=repr(error))
        elif not isinstance(error, app_commands.CheckFailure | app_commands.NoPrivateMessage):
            command = getattr(getattr(interaction, "command", None), "qualified_name", None)
            try:
                log.exception(
                    "app_command_error",
                    error=repr(error),
                    code=error_code(interaction),
                    guild_id=None if interaction.guild is None else interaction.guild.id,
                    user_id=getattr(getattr(interaction, "user", None), "id", None),
                    action=command,
                )
            except Exception:
                log.exception("app_command_error", error=repr(error))
        try:
            await respond(interaction, command_failure(interaction))
        except Exception:
            log.warning("command_error_unsent", error=repr(error))

    async def on_error(self, event_method: str, /, *args: object, **kwargs: object) -> None:
        reporter = getattr(self, "errors", None)
        report = getattr(reporter, "report_listener", None)
        if callable(report):
            try:
                await report(event_method, sys.exc_info()[1] or Exception(event_method), *args)
                return
            except Exception:
                log.exception("critical_listener_report_failed", event=event_method)
        log.exception("listener_failed", event=event_method)

    async def _sync_application_commands(self) -> None:
        fence = self.fence
        if fence is None:
            log.warning("commands_sync_skipped", reason="guild fence missing")
            return
        scope = fence.sync_scope()
        path = command_hash_path(self.settings)
        previous = read_command_hash(path)
        force = self.settings.force_sync
        try:
            digest = tree_fingerprint(snapshot_tree(self.tree), scope=scope)
        except Exception:
            log.exception("command_snapshot_failed")
            digest = ""
            force = True
        if not force and previous == digest:
            log.info("commands_unchanged", hash=digest[:12], path=str(path), scope=scope)
            return
        try:
            for guild_id in sorted(fence.active_ids):
                target = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=target)
                synced = await self.tree.sync(guild=target)
                log.info(
                    "commands_synced_guild",
                    count=len(synced),
                    guild_id=guild_id,
                    hash=digest[:12],
                    forced=force,
                )
            for guild_id in sorted(fence.resident_ids - fence.active_ids):
                silent = discord.Object(id=guild_id)
                self.tree.clear_commands(guild=silent)
                await self.tree.sync(guild=silent)
                log.info("commands_cleared_guild", guild_id=guild_id)
            if self.application_id is not None:
                await self.http.bulk_upsert_global_commands(self.application_id, [])
                log.info("commands_cleared_global")
        except Exception:
            log.exception("commands_sync_failed")
            return
        try:
            write_command_hash(path, digest)
        except OSError:
            log.exception("command_hash_write_failed", path=str(path))

    async def on_ready(self) -> None:
        await self.enforce_guild_fence()
        status = "тест" if self.fence is not None and self.fence.mode is BotMode.TEST else "conductor"
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=status))
        self.log_pump.start()
        for guild in self.guilds:
            if not self.serves(guild.id):
                continue
            try:
                await self._restore_voice(guild)
            except Exception:
                log.exception("voice_restore_failed", guild_id=guild.id)
            try:
                await sweep_orphaned_special(self, guild)
            except Exception:
                log.exception("access_unlock_failed", guild_id=guild.id)
        if self._booted:
            log.debug(
                "ready",
                user=str(self.user),
                guilds=len(self.guilds),
                mode=self.settings.mode_label,
            )
            return
        self._booted = True
        for guild in self.guilds:
            if not self.serves(guild.id):
                continue
            try:
                await self._bootstrap_guild(guild)
            except Exception:
                # One unreachable guild must not stop the boot of the others.
                log.exception("guild_bootstrap_failed", guild_id=guild.id)
        try:
            self.automod_rules.flush_notices()
        except Exception:
            log.exception("automod_rules_notice_failed")
        self._emit_startup_ready()

    async def on_resumed(self) -> None:
        self._resume_at.append(time.monotonic())

    def _emit_startup_ready(self) -> None:
        if self._startup_ready:
            return
        self._startup_ready = True
        profile = getattr(getattr(self, "traffic", None), "profile", None)
        traffic = getattr(profile, "value", None) or str(getattr(self.settings, "rate_profile", ""))
        ms = int((time.perf_counter() - self._boot_started) * 1000)
        log.info(
            "startup_ready",
            version=__version__,
            guilds=len(self.guilds),
            traffic=str(traffic),
            alembic=script_head(),
            ms=ms,
        )

    async def _bootstrap_guild(self, guild: discord.Guild) -> None:
        await self.refresh_config(guild.id)
        result = await self.run_census(guild)
        await self.invites.refresh(guild)
        await self.history.load(self.sessions, guild.id)
        log.info("auto_census", guild_id=guild.id, humans=result.humans, boosters=result.boosters)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not self.may_reside(guild.id):
            log.warning("rejecting_guild", guild_id=guild.id, name=guild.name)
            try:
                await guild.leave()
            except discord.HTTPException:
                log.exception("guild_leave_failed", guild_id=guild.id)
            return
        if not self.serves(guild.id):
            log.info("ignoring_inactive_guild", guild_id=guild.id, mode=self.settings.mode_label)
            return
        try:
            await self.refresh_config(guild.id)
            await self.run_census(guild)
            await self.invites.refresh(guild)
            await self.history.load(self.sessions, guild.id)
            await self._restore_voice(guild)
        except Exception:
            log.exception("guild_join_failed", guild_id=guild.id)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if roles_changed((role.id for role in before.roles), (role.id for role in after.roles)):
            self.caches.invalidate_member(after.guild.id, after.id)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        self.caches.invalidate_access(after.guild.id)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        self.caches.invalidate_access(role.guild.id)
        try:
            await sweep_orphaned_special(self, role.guild)
        except Exception:
            log.exception("access_unlock_failed", guild_id=role.guild.id)

    async def _restore_voice(self, guild: discord.Guild) -> None:
        await self.voice_sessions.restore(
            self.sessions,
            guild_id=guild.id,
            live=live_voices(guild),
            now=datetime.now(UTC),
        )
        try:
            await self.rooms.restore(guild)
        except Exception:
            log.exception("voice_rooms_restore_failed", guild_id=guild.id)
        try:
            await self.moderation.restore_locks(guild)
        except Exception:
            log.exception("mod_locks_restore_failed", guild_id=guild.id)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.scheduler.stop()
        self.queue.stop()
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # Cancelling is not enough: without awaiting, a task can still touch a disposed engine.
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.collector.flush(self.sessions)
            await self.history.flush(self.sessions)
        except Exception:
            log.exception("final_flush_failed")
        await self.log_pump.aclose()
        session = self.http_session
        self.http_session = None
        if session is not None:
            await session.close()
        if self._mutation_lock.installed:
            self._mutation_lock.uninstall()
        await self.engine.dispose()
        await super().close()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        try:
            task = asyncio.create_task(coro, name=name)
        except RuntimeError:
            log.exception("spawn_failed", task=name)
            coro.close()
            raise
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_background_done)
        return task

    def _on_background_done(self, task: asyncio.Task[Any]) -> None:
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        if task.get_name() == "critical-task-log":
            log.error(
                "background_task_failed",
                task=task.get_name(),
                error=repr(exc),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        reporter = getattr(self, "errors", None)
        report = getattr(reporter, "report_task", None)
        if callable(report):
            try:
                self.spawn(report(task.get_name(), exc), name="critical-task-log")
                return
            except Exception:
                pass
        log.error(
            "background_task_failed",
            task=task.get_name(),
            error=repr(exc),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    async def decide_access(
        self,
        *,
        guild_id: int,
        user_id: int,
        member_role_ids: object,
        is_administrator: bool,
        key: str,
        now: datetime | None = None,
    ):
        roles = tuple(member_role_ids) if isinstance(member_role_ids, Iterable) else ()
        return await cached_decide(
            self,
            guild_id=guild_id,
            user_id=user_id,
            member_role_ids=roles,
            is_administrator=is_administrator,
            key=key,
            now=now,
        )

    async def refresh_config(self, guild_id: int) -> GuildConfig:
        async with session_scope(self.sessions) as session:
            config = await get_or_create_config(session, guild_id)
            await ensure_defaults(session, guild_id)
        self.config_by_guild[guild_id] = config
        remember_tz(guild_id, config.timezone)
        self.roles.bind(guild_id, RoleSubsystem.LOYALTY, old_role_id_set(config))
        await self.moderation.bind_mute(guild_id)
        await self.reputation.bind_tiers(guild_id)
        await self.achievements.bind_roles(guild_id)
        await self.ladder.bind_roles(guild_id)
        await self.giveaways.bind_roles(guild_id)
        await self.verification.bind(guild_id)
        await self.self_roles.bind(guild_id)
        return config

    def forget_voice(self, guild_id: int, user_id: int) -> None:
        self.voice_sessions.forget_memory(guild_id, user_id)
        self.last_used_invite.pop(user_id, None)

    async def run_census(self, guild: discord.Guild) -> CensusResult:
        if not guild.chunked:
            await guild.chunk()
        now = datetime.now(UTC)
        async with session_scope(self.sessions) as session:
            config = await get_or_create_config(session, guild.id)
            census_at = config.census_at
        snapshots: list[MemberSnapshot] = []
        humans = bots = boosters = 0
        for member in guild.members:
            joined_at = member.joined_at
            if joined_at is None:
                continue
            snapshots.append(
                MemberSnapshot(
                    user_id=member.id,
                    username=str(member),
                    display_name=member.display_name,
                    is_bot=member.bot,
                    joined_at=joined_at,
                    account_created_at=member.created_at,
                    booster_since=member.premium_since,
                )
            )
            if member.bot:
                bots += 1
            else:
                humans += 1
                if member.premium_since:
                    boosters += 1
        counts = await apply_census(
            self.sessions,
            guild_id=guild.id,
            members=snapshots,
            census_at=census_at,
            now=now,
        )
        async with session_scope(self.sessions) as session:
            config = await get_or_create_config(session, guild.id)
            config.census_at = now
        await self.refresh_config(guild.id)
        return CensusResult(
            total=humans + bots,
            humans=humans,
            bots=bots,
            boosters=boosters,
            created=counts.created,
            updated=counts.updated,
        )
