from __future__ import annotations

from typing import TYPE_CHECKING

import discord
import structlog
from discord import app_commands
from discord.ext import commands, tasks

from conductor.db.session import session_scope
from conductor.domain.access import LOYALTY_MANAGE
from conductor.domain.loyalty import old_role_change
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import edit_response, error_code, respond, update_message
from conductor.presentation.loyalty import (
    TAB_REGISTRY,
    TAB_WIZARD,
    candidates_card,
    empty_census_card,
    frozen_card,
    grant_confirm_card,
    grant_denial_view,
    granted_card,
    need_roles_card,
    preview_file,
    preview_summary,
    registry_fields,
    wizard_card,
)
from conductor.presentation.motion import InteractionTarget, Progress
from conductor.services.guild_config import get_or_create_config
from conductor.services.loyalty import LoyaltyPlan, build_plan, grouped_assignments, save_confirmed_batch
from conductor.services.old_roles import count_registry_mismatches, load_registry, old_role_id_set, registry_counts

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.loyalty")


class Loyalty(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot
        self._swept: set[int] = set()
        self._audit_denied: set[int] = set()
        self._pending_roles: dict[int, list[tuple[discord.Member, set[int], set[int]]]] = {}

    async def cog_load(self) -> None:
        self._watch_registry.start()

    async def cog_unload(self) -> None:
        self._watch_registry.cancel()

    @tasks.loop(minutes=30)
    async def _watch_registry(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self.bot.loyalty.protect_guild(guild)
            except Exception:
                log.exception("loyalty_registry_watch_failed", guild_id=guild.id)

    @_watch_registry.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            if guild.id in self._swept:
                continue
            self._swept.add(guild.id)
            self.bot.spawn(self.bot.loyalty.protect_guild(guild), name=f"loyalty-protect-{guild.id}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self.bot.loyalty.enforce_member(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot:
            return
        config = self.bot.config_by_guild.get(after.guild.id)
        old_ids = old_role_id_set(config)
        if not old_ids:
            return
        granted, removed = old_role_change(
            {role.id for role in before.roles},
            {role.id for role in after.roles},
            old_ids,
        )
        if not granted and not removed:
            return
        pending = self._pending_roles.setdefault(after.guild.id, [])
        pending.append((after, granted, removed))
        key = ("loyalty-audit", after.guild.id)
        if self.bot.queue.debounce(key, 5.0):
            self.bot.queue.schedule_later(
                ("loyalty-audit-flush", after.guild.id),
                5.0,
                lambda: self._flush_role_audit(after.guild.id),
            )

    async def protect_guild(self, guild: discord.Guild) -> None:
        await self.bot.loyalty.protect_guild(guild)

    async def _flush_role_audit(self, guild_id: int) -> None:
        items = self._pending_roles.pop(guild_id, [])
        if not items:
            return
        guild = items[0][0].guild
        actors = await self._role_actors(guild)
        for member, granted, removed in items:
            actor = actors.get(member.id)
            await self.bot.loyalty.enforce_member(
                member,
                actor_id=None if actor is None else actor.id,
                granted=granted,
                removed=removed,
            )

    async def _role_actors(self, guild: discord.Guild) -> dict[int, discord.abc.User]:
        found: dict[int, discord.abc.User] = {}
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return found
        if guild.id in self._audit_denied:
            return found
        try:
            async for entry in guild.audit_logs(limit=20, action=discord.AuditLogAction.member_role_update):
                target_id = getattr(entry.target, "id", None)
                if target_id is None or entry.user is None:
                    continue
                if me is not None and entry.user.id == me.id:
                    continue
                found.setdefault(int(target_id), entry.user)
        except discord.Forbidden:
            self._audit_denied.add(guild.id)
            log.warning("audit_log_forbidden", guild_id=guild.id)
        except discord.HTTPException:
            return found
        return found

    @app_commands.command(name="loyalty", description=copy.CMD_LOYALTY)
    @app_commands.guild_only()
    @keyed(LOYALTY_MANAGE)
    async def loyalty(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        await respond(interaction, await self._home(guild, TAB_REGISTRY))

    async def _home(self, guild: discord.Guild, tab: str) -> discord.ui.LayoutView:
        if tab == TAB_WIZARD:
            return wizard_card(
                tab=TAB_WIZARD,
                title=copy.LOYALTY_GRANT_TITLE,
                body=copy.LOYALTY_GRANT_FOOT,
                on_tab=self._switch,
                on_candidates=self._candidates,
                on_preview=self._preview,
                on_freeze=self._freeze,
                on_grant=self._grant,
            )
        async with session_scope(self.bot.sessions) as session:
            config = await get_or_create_config(session, guild.id)
            counts = await registry_counts(session, guild.id)
            mapping = await load_registry(session, guild.id)
            seeded_at = config.loyalty_registry_seeded_at
        members = [(member.id, {role.id for role in member.roles}, member.bot) for member in guild.members]
        mismatches = count_registry_mismatches(members, config, mapping)
        return wizard_card(
            tab=TAB_REGISTRY,
            title=copy.LOYALTY_REGISTRY_TITLE,
            body=copy.LOYALTY_REGISTRY_BODY,
            fields=registry_fields(
                counts=counts,
                mismatches=mismatches,
                seeded_at=seeded_at,
                old_ids=old_role_id_set(config),
            ),
            on_tab=self._switch,
        )

    async def _switch(self, interaction: discord.Interaction, tab: str) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not await ack(interaction):
            return
        await edit_response(interaction, await self._home(guild, tab))

    async def _plan(self, guild: discord.Guild) -> LoyaltyPlan:
        async with session_scope(self.bot.sessions) as session:
            return await build_plan(
                session,
                guild_id=guild.id,
                guild_created_at=guild.created_at,
            )

    async def _loaded_plan(self, interaction: discord.Interaction, guild: discord.Guild) -> LoyaltyPlan | None:
        plan = await self._plan(guild)
        if plan.considered == 0:
            await _show(interaction, empty_census_card())
            return None
        return plan

    async def _candidates(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not await ack(interaction):
            return
        plan = await self._loaded_plan(interaction, guild)
        if plan is None:
            return
        await _show(interaction, candidates_card(guild, plan))

    async def _preview(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not await ack(interaction):
            return
        plan = await self._loaded_plan(interaction, guild)
        if plan is None:
            return
        await _show(interaction, preview_summary(plan))
        file = preview_file(guild, grouped_assignments(plan))
        try:
            await interaction.followup.send(file=file, ephemeral=True)
        except discord.HTTPException as exc:
            log.warning("loyalty_preview_send_failed", error=str(exc))

    async def _freeze(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not await ack(interaction):
            return
        plan = await self._loaded_plan(interaction, guild)
        if plan is None:
            return
        async with session_scope(self.bot.sessions) as session:
            batch = await save_confirmed_batch(
                session,
                guild_id=guild.id,
                created_by=interaction.user.id,
                plan=plan,
            )
        await _show(interaction, frozen_card(batch.id, plan))

    async def _grant(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not await ack(interaction):
            return
        denial = await self.bot.loyalty.decide_grant(guild.id)
        if denial is not None:
            await _show(interaction, grant_denial_view(denial))
            return

        async def on_yes(confirm: discord.Interaction) -> None:
            await self._apply(confirm)

        async def on_no(confirm: discord.Interaction) -> None:
            if not await ack(confirm):
                return
            await edit_response(confirm, await self._home(guild, TAB_WIZARD))

        await _show(interaction, grant_confirm_card(on_yes=on_yes, on_no=on_no))

    async def _apply(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await _show(interaction, need_roles_card())
            return
        if not await ack(interaction):
            return
        progress = Progress(
            InteractionTarget(interaction),
            title=copy.LOYALTY_GRANT_TITLE,
            body=copy.LOYALTY_GRANT_FOOT,
            registry=self.bot.motion,
            code=error_code(interaction),
            show_after=0,
        )
        async with progress:
            await progress.start()
            prep = await self.bot.loyalty.prepare_grant(guild, me)
            if prep.denial is not None:
                await progress.finish(grant_denial_view(prep.denial))
                return
            assert prep.plan is not None and prep.batch_id is not None
            stats = await self.bot.loyalty.apply_plan(guild, prep.plan, prep.role_ids)
            await self.bot.loyalty.mark_applied(prep.batch_id, interaction.user.id)
            await progress.finish(granted_card(prep.batch_id, stats))


async def _show(interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
    if interaction.response.is_done():
        await edit_response(interaction, view)
        return
    await update_message(interaction, view)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Loyalty(bot))
