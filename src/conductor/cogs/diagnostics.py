"""Самопроверка: вкладки «настройка», «каркас», «готовность» и «сейчас»."""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.db.session import session_scope
from conductor.domain.access import CONFIG_MANAGE, DIAGNOSE_RUN, scheme_drift_keys
from conductor.domain.observe import TAB_READY, TAB_SETUP, TAB_SKELETON
from conductor.domain.seed import SeedMode, SkeletonStatus
from conductor.presentation import copy
from conductor.presentation.access import allow_key, keyed
from conductor.presentation.diagnose import (
    CheckStatus,
    CreateHoldView,
    DiagnoseLine,
    DiagnoseSnapshot,
    LogChannelView,
    ManagedRoleView,
    apply_defaults_confirm,
    build_diagnose_lines,
    diagnose_card,
    pack_create_holds,
    pack_observer,
    ready_needs_fix,
    render_diagnose,
    restore_perms_confirm,
    skeleton_page_count,
)
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, edit_response, respond, update_message
from conductor.services.access import apply_scheme_defaults, cached_snapshot, restore_defaults
from conductor.services.create_lock import snapshot_holds
from conductor.services.observe import collect_diagnose_snapshot

if TYPE_CHECKING:
    from conductor.bot import Conductor
    from conductor.db.models import GuildConfig

__all__ = [
    "CheckStatus",
    "DiagnoseLine",
    "DiagnoseSnapshot",
    "LogChannelView",
    "ManagedRoleView",
    "build_diagnose_lines",
    "collect_diagnose_snapshot",
    "diagnose_card",
    "render_diagnose",
]


class Diagnostics(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    @app_commands.command(
        name="diagnose",
        description=copy.CMD_DIAGNOSE,
    )
    @app_commands.guild_only()
    @keyed(DIAGNOSE_RUN)
    async def diagnose(self, interaction: discord.Interaction) -> None:
        await self._reply(interaction, TAB_SETUP)

    async def _reply(self, interaction: discord.Interaction, tab: str, *, page: int = 1) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        view = await self._card(guild, tab, page=page)
        await edit_response(interaction, view)

    async def _card(self, guild: discord.Guild, tab: str, *, page: int = 1):
        snap = await self._snapshot(guild)
        on_apply = (
            self._apply_defaults
            if tab == TAB_SETUP and (snap.scheme_drift or snap.rep_defaults or snap.ladder_defaults)
            else None
        )
        broken = tuple(item for item in snap.skeleton if item.status in {SkeletonStatus.WARN, SkeletonStatus.FAIL})
        needs_fix = bool(broken) or ready_needs_fix(snap)
        pages = 1
        if tab == TAB_SKELETON:
            pages = skeleton_page_count(snap)
            page = max(1, min(int(page), pages))
        return diagnose_card(
            build_diagnose_lines(snap, tab=tab, page=page),
            tab=tab,
            on_tab=self._switch,
            on_apply=on_apply,
            on_restore=self._restore_perms if tab == TAB_SETUP else None,
            on_repair=self._repair if tab == TAB_SKELETON and broken else None,
            on_repair_all=self._repair_all if tab in {TAB_SKELETON, TAB_READY} and needs_fix else None,
            on_reapply=self._reapply if tab in {TAB_SKELETON, TAB_READY} and needs_fix else None,
            on_map=self._map if tab in {TAB_SKELETON, TAB_READY} and needs_fix else None,
            on_page=self._page if tab == TAB_SKELETON and pages > 1 else None,
            broken=broken,
            page=page,
            pages=pages,
        )

    async def _page(self, interaction: discord.Interaction, page: int) -> None:
        await self._reply(interaction, TAB_SKELETON, page=page)

    async def _repair_all(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if not await allow_key(interaction, CONFIG_MANAGE):
            return
        if not await ack(interaction):
            return
        run = getattr(getattr(interaction.client, "seed", None), "run", None)
        if callable(run):
            await run(interaction.guild, actor_id=interaction.user.id, mode=SeedMode.BUILD)
        view = await self._card(interaction.guild, TAB_SKELETON)
        await edit_response(interaction, view)

    async def _reapply(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if not await allow_key(interaction, CONFIG_MANAGE):
            return
        if not await ack(interaction):
            return
        reapply = getattr(getattr(interaction.client, "seed", None), "reapply_rights", None)
        if callable(reapply):
            await reapply(interaction.guild, actor_id=interaction.user.id)
        view = await self._card(interaction.guild, TAB_SKELETON)
        await edit_response(interaction, view)

    async def _map(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if not await allow_key(interaction, CONFIG_MANAGE):
            return
        opener = getattr(getattr(interaction.client, "setup_map", None), "opener", None)
        if not callable(opener):
            return
        await opener(actor_id=int(interaction.user.id))(interaction)

    async def _repair(self, interaction: discord.Interaction, key: str) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if not await allow_key(interaction, CONFIG_MANAGE):
            return
        if not await ack(interaction):
            return
        repair = getattr(getattr(interaction.client, "seed", None), "repair", None)
        if callable(repair):
            await repair(interaction.guild, key, actor_id=interaction.user.id)
        view = await self._card(interaction.guild, TAB_SKELETON)
        await edit_response(interaction, view)

    async def _apply_defaults(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if not await allow_key(interaction, CONFIG_MANAGE):
            return

        async def yes(click: discord.Interaction) -> None:
            guild = click.guild
            if guild is None:
                await respond(click, denied(copy.PANEL_GUILD_ONLY))
                return
            if not await ack(click):
                return
            async with session_scope(self.bot.sessions) as session:
                await apply_scheme_defaults(session, guild.id)
            apply_rep = getattr(self.bot.reputation, "apply_new_defaults", None)
            if callable(apply_rep):
                await apply_rep(guild.id)
            apply_ladder = getattr(self.bot.ladder, "apply_new_defaults", None)
            if callable(apply_ladder):
                await apply_ladder(guild.id)
            self.bot.caches.invalidate_access(guild.id)
            await self._reply(click, TAB_SETUP)

        async def no(click: discord.Interaction) -> None:
            await self._reply(click, TAB_SETUP)

        await update_message(interaction, apply_defaults_confirm(on_yes=yes, on_no=no))

    async def _restore_perms(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if not await allow_key(interaction, CONFIG_MANAGE):
            return

        async def yes(click: discord.Interaction) -> None:
            guild = click.guild
            if guild is None:
                await respond(click, denied(copy.PANEL_GUILD_ONLY))
                return
            if not await ack(click):
                return
            async with session_scope(self.bot.sessions) as session:
                await restore_defaults(session, guild.id)
            self.bot.caches.invalidate_access(guild.id)
            await self._reply(click, TAB_SETUP)

        async def no(click: discord.Interaction) -> None:
            await self._reply(click, TAB_SETUP)

        await update_message(interaction, restore_perms_confirm(on_yes=yes, on_no=no))

    async def _switch(self, interaction: discord.Interaction, tab: str) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        await self._reply(interaction, tab)

    async def _snapshot(self, guild: discord.Guild) -> DiagnoseSnapshot:
        config: GuildConfig | None = self.bot.config_by_guild.get(guild.id)
        if config is None:
            config = await self.bot.refresh_config(guild.id)
        at_threshold, would_ban = await self.bot.reputation.diagnose_counts(guild.id)
        db_ok = True
        observe = getattr(self.bot, "observe", None)
        ping = getattr(observe, "ping_db", None)
        if callable(ping):
            db_ok = await ping()
        access = await cached_snapshot(self.bot, guild.id)
        quota_capped = 0
        capped = getattr(self.bot.moderation, "quota_capped_count", None)
        if callable(capped):
            quota_capped = await capped(guild)
        evidence_category = None
        evidence_archive = None
        evidence_open = 0
        evidence_overdue = 0
        health = getattr(getattr(self.bot, "evidence", None), "health", None)
        if callable(health):
            denial, opened, overdue = await health(guild.id, guild=guild)
            evidence_category = "ok" if denial is None else str(getattr(denial, "value", denial))
            evidence_open = int(opened or 0)
            evidence_overdue = int(overdue or 0)
        archive_probe = getattr(getattr(self.bot, "evidence", None), "archive_status", None)
        if callable(archive_probe):
            evidence_archive = await archive_probe(guild.id, guild=guild)
        automod_card = None
        status = getattr(getattr(self.bot, "automod", None), "card_status", None)
        if callable(status):
            state = await status(guild.id)
            if state != "off":
                automod_card = state == "live"
        verification_writable = None
        writable = getattr(getattr(self.bot, "verification", None), "trap_writable", None)
        if callable(writable):
            verification_writable = writable(guild)
        stats_board = None
        status = getattr(getattr(self.bot, "stats_board", None), "card_status", None)
        if callable(status):
            stats_board = await status(guild.id)
        youtube_feeds = None
        youtube_last_error = None
        youtube_last_checked_at = None
        health = getattr(getattr(self.bot, "youtube", None), "health", None)
        if callable(health):
            report = await health(guild.id)
            youtube_feeds = int(getattr(report, "feeds", 0) or 0)
            youtube_last_error = getattr(report, "last_error", None)
            youtube_last_checked_at = getattr(report, "last_checked_at", None)
        lfg_card = None
        status = getattr(getattr(self.bot, "lfg", None), "card_status", None)
        if callable(status):
            lfg_card = await status(guild.id)
        voice_hub = None
        hub_denial = None
        probe = getattr(getattr(self.bot, "rooms", None), "hub_health", None)
        if callable(probe):
            denial, _details = await probe(guild)
            voice_hub = "ok" if denial is None else str(getattr(denial, "value", denial))
            hub_denial = None if denial is None else str(getattr(denial, "value", denial))
        evidence_denial = None
        if evidence_category not in {None, "ok", True, False}:
            evidence_denial = str(evidence_category)
        elif evidence_category not in {None, "ok", True}:
            evidence_denial = "no_category"
        showcases: dict[str, str] = {}
        statuses = getattr(getattr(self.bot, "showcase", None), "statuses", None)
        if callable(statuses):
            found = await statuses(guild)
            showcases = {str(key): str(getattr(value, "value", value)) for key, value in dict(found).items()}
        structure_channel = False
        raw = None if config is None else getattr(config, "structure_channel_id", None)
        if raw is not None:
            getter = getattr(guild, "get_channel", None)
            structure_channel = callable(getter) and getter(int(raw)) is not None
        ticket_log = None
        probe = getattr(getattr(self.bot, "tickets", None), "diagnose_log", None)
        if callable(probe):
            ticket_log = await probe(guild)
        rep_defaults = False
        drift = getattr(self.bot.reputation, "defaults_drift", None)
        if callable(drift):
            rep_defaults = await drift(guild.id)
        ladder_defaults = False
        ladder_drift = getattr(self.bot.ladder, "defaults_drift", None)
        if callable(ladder_drift):
            ladder_defaults = await ladder_drift(guild.id)
        suspended_watchers = 0
        count = getattr(getattr(self.bot, "watchers_report", None), "count_active", None)
        if callable(count):
            suspended_watchers = await count(guild.id)
        schema_revision = None
        schema_head = None
        revisions = getattr(observe, "schema_revisions", None)
        if callable(revisions):
            schema_revision, schema_head = await revisions()
        create_holds: tuple[CreateHoldView, ...] = ()
        lister = snapshot_holds
        if callable(lister):
            moment = time()
            create_holds = pack_create_holds(lister(self.bot, guild.id, now=moment), now=moment)
        rate_observed, traffic = pack_observer(getattr(self.bot, "rate_observer", None), guild.id, now=time())
        return collect_diagnose_snapshot(
            self.bot,
            guild,
            config,
            rep_at_threshold=at_threshold,
            rep_would_ban=would_ban,
            ladder_roles=await self._ladder_roles(guild),
            db_ok=db_ok,
            scheme_drift=len(scheme_drift_keys(access)),
            quota_capped=quota_capped,
            suspended_watchers=suspended_watchers,
            evidence_category=evidence_category,
            evidence_archive=evidence_archive,
            evidence_open=evidence_open,
            evidence_overdue=evidence_overdue,
            automod_card=automod_card,
            verification_writable=verification_writable,
            stats_board=stats_board,
            youtube_feeds=youtube_feeds,
            youtube_last_error=youtube_last_error,
            youtube_last_checked_at=youtube_last_checked_at,
            lfg_card=lfg_card,
            voice_hub=voice_hub,
            hub_denial=hub_denial,
            evidence_denial=evidence_denial,
            showcases=showcases,
            structure_channel=structure_channel,
            ticket_log=ticket_log,
            rep_defaults=rep_defaults,
            ladder_defaults=ladder_defaults,
            skeleton=await self._skeleton(guild),
            schema_revision=schema_revision,
            schema_head=schema_head,
            create_holds=create_holds,
            rate_observed=rate_observed,
            traffic=traffic,
        )

    async def _skeleton(self, guild: discord.Guild):
        probe = getattr(getattr(self.bot, "seed", None), "diagnose_checks", None)
        if not callable(probe):
            return ()
        return await probe(guild)

    async def _ladder_roles(self, guild: discord.Guild) -> tuple[ManagedRoleView, ...]:
        bound = await self.bot.ladder.bound_roles(guild.id)
        found: list[ManagedRoleView] = []
        for key, role_id in bound:
            role = guild.get_role(role_id)
            found.append(
                ManagedRoleView(
                    label=copy.ladder_step(key),
                    position=None if role is None else int(role.position),
                )
            )
        return tuple(found)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Diagnostics(bot))
