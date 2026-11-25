"""Команды владельца бота: wipe, массовые роли, импорт Old, каркас теста."""

from time import time

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

from conductor.bot import Conductor
from conductor.domain.loyalty import LoyaltyTier
from conductor.domain.ops import MassMode, wipe_section
from conductor.presentation import copy
from conductor.presentation.blueprint import (
    APPLY_MODE_CHOICES,
    EXPORT_SECTION_CHOICES,
    run_blueprint_apply,
    run_blueprint_export,
)
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, empty, respond
from conductor.presentation.motion import InteractionTarget
from conductor.presentation.ops import (
    GRANT_DESCRIBE,
    MATCH_CHOICES,
    PROTECT_CHOICES,
    SEED_MODE_CHOICES,
    STRIP_DESCRIBE,
    TIER_CHOICES,
    WIPE_CHOICES,
    dismiss_owner,
    grant_confirm,
    lift_done,
    mass_done,
    old_import_confirm,
    old_import_view,
    owner_click,
    rep_done,
    run_owner_limits,
    run_owner_progress,
    run_owner_seed,
    show_emoji_deploy,
    strip_confirm,
    wipe_confirm,
    wipe_done,
)
from conductor.services.admin_pages import log_change
from conductor.services.create_lock import clear_guild, deploy_emojis, snapshot_holds
from conductor.services.ops import bound_tier, match_mode, protect_kind, unique_roles


@app_commands.guild_only()
class Owner(commands.GroupCog, group_name="owner"):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    async def _owner(self, interaction: discord.Interaction) -> bool:
        if self.bot.is_developer(interaction.user.id):
            return True
        await respond(interaction, denied(copy.OWNER_DENIED, need=copy.OWNER_NEED))
        return False

    def _same_owner(self, click: discord.Interaction, actor_id: int) -> bool:
        return click.user.id == actor_id and self.bot.is_developer(click.user.id)

    def _choice(self, value: Choice[str] | None) -> str | None:
        return None if value is None else value.value

    @app_commands.command(name="wipe", description=copy.CMD_OWNER_WIPE)
    @app_commands.describe(section=copy.CMD_OWNER_SECTION)
    @app_commands.choices(section=WIPE_CHOICES)
    async def wipe(self, interaction: discord.Interaction, section: Choice[str]) -> None:
        if not await self._owner(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        resolved = wipe_section(section.value)
        if resolved is None:
            await respond(interaction, empty(copy.OWNER_WIPE_EMPTY))
            return
        actor_id = interaction.user.id

        async def yes(click: discord.Interaction) -> None:
            if not await owner_click(click, self._same_owner, actor_id):
                return
            if not await ack(click):
                return
            report = await self.bot.ops.wipe(guild.id, resolved)
            await InteractionTarget(click).edit(wipe_done(section=report.section, rows=report.rows))

        cancel = dismiss_owner(self._same_owner, actor_id, copy.OWNER_WIPE_TITLE)
        await respond(interaction, wipe_confirm(section=resolved, on_yes=yes, on_no=cancel))

    @app_commands.command(name="strip", description=copy.CMD_OWNER_STRIP)
    @app_commands.describe(**STRIP_DESCRIBE)
    @app_commands.choices(have_mode=MATCH_CHOICES, without_mode=MATCH_CHOICES, protect=PROTECT_CHOICES)
    async def strip(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        role2: discord.Role | None = None,
        role3: discord.Role | None = None,
        all_roles: bool = False,
        have: discord.Role | None = None,
        have2: discord.Role | None = None,
        have3: discord.Role | None = None,
        have_mode: Choice[str] | None = None,
        without: discord.Role | None = None,
        without2: discord.Role | None = None,
        without3: discord.Role | None = None,
        without_mode: Choice[str] | None = None,
        protect: Choice[str] | None = None,
        skip_timeout: bool = False,
        skip_pending: bool = False,
        skip_boost: bool = False,
    ) -> None:
        if not await self._owner(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        selected = unique_roles(role, role2, role3)
        if all_roles == bool(selected):
            await respond(interaction, denied(copy.OWNER_ROLES_MODE))
            return
        preview = await self.bot.ops.preview_strip(
            guild,
            selected=selected,
            all_roles=all_roles,
            require=unique_roles(have, have2, have3),
            require_mode=match_mode(self._choice(have_mode)),
            exclude=unique_roles(without, without2, without3),
            exclude_mode=match_mode(self._choice(without_mode)),
            protect=protect_kind(self._choice(protect)),
            skip_timeout=skip_timeout,
            skip_pending=skip_pending,
            skip_boost=skip_boost,
        )
        if preview.members <= 0:
            await respond(interaction, empty(copy.OWNER_MEMBERS_NONE))
            return
        actor_id = interaction.user.id

        async def yes(click: discord.Interaction) -> None:
            if not await owner_click(click, self._same_owner, actor_id):
                return
            await run_owner_progress(
                click,
                title=copy.OWNER_STRIP_TITLE,
                registry=self.bot.motion,
                run=lambda on_progress: self.bot.ops.strip(
                    guild,
                    actor_id=actor_id,
                    roles=preview.selected,
                    all_roles=all_roles,
                    audience=preview.audience,
                    protect=preview.kind,
                    on_progress=on_progress,
                ),
                done=lambda report: mass_done(report, grant=False),
                unit=copy.PROGRESS_UNIT_MEMBERS,
            )

        cancel = dismiss_owner(self._same_owner, actor_id, copy.OWNER_STRIP_TITLE)
        await respond(
            interaction,
            strip_confirm(
                mode=MassMode.ALL if all_roles else MassMode.SELECTED,
                roles=preview.labels,
                members=preview.members,
                filters=preview.filters,
                on_yes=yes,
                on_no=cancel,
            ),
        )

    @app_commands.command(name="grant", description=copy.CMD_OWNER_GRANT)
    @app_commands.describe(**GRANT_DESCRIBE)
    @app_commands.choices(have_mode=MATCH_CHOICES, without_mode=MATCH_CHOICES, protect=PROTECT_CHOICES)
    async def grant(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        role2: discord.Role | None = None,
        role3: discord.Role | None = None,
        have: discord.Role | None = None,
        have2: discord.Role | None = None,
        have3: discord.Role | None = None,
        have_mode: Choice[str] | None = None,
        without: discord.Role | None = None,
        without2: discord.Role | None = None,
        without3: discord.Role | None = None,
        without_mode: Choice[str] | None = None,
        protect: Choice[str] | None = None,
        skip_timeout: bool = False,
        skip_pending: bool = False,
        skip_boost: bool = False,
    ) -> None:
        if not await self._owner(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        preview = await self.bot.ops.preview_grant(
            guild,
            selected=unique_roles(role, role2, role3),
            require=unique_roles(have, have2, have3),
            require_mode=match_mode(self._choice(have_mode)),
            exclude=unique_roles(without, without2, without3),
            exclude_mode=match_mode(self._choice(without_mode)),
            protect=protect_kind(self._choice(protect)),
            skip_timeout=skip_timeout,
            skip_pending=skip_pending,
            skip_boost=skip_boost,
        )
        if preview.members <= 0:
            await respond(interaction, empty(copy.OWNER_MEMBERS_NONE))
            return
        actor_id = interaction.user.id

        async def yes(click: discord.Interaction) -> None:
            if not await owner_click(click, self._same_owner, actor_id):
                return
            await run_owner_progress(
                click,
                title=copy.OWNER_GRANT_TITLE,
                registry=self.bot.motion,
                run=lambda on_progress: self.bot.ops.grant(
                    guild,
                    actor_id=actor_id,
                    roles=preview.selected,
                    audience=preview.audience,
                    protect=preview.kind,
                    on_progress=on_progress,
                ),
                done=lambda report: mass_done(report, grant=True),
                unit=copy.PROGRESS_UNIT_MEMBERS,
            )

        cancel = dismiss_owner(self._same_owner, actor_id, copy.OWNER_GRANT_TITLE)
        await respond(
            interaction,
            grant_confirm(
                roles=preview.labels,
                members=preview.members,
                filters=preview.filters,
                on_yes=yes,
                on_no=cancel,
            ),
        )

    @app_commands.command(name="old-import", description=copy.CMD_OWNER_OLD_IMPORT)
    @app_commands.describe(role=copy.CMD_OWNER_ROLE, tier=copy.CMD_OWNER_TIER)
    @app_commands.choices(tier=TIER_CHOICES)
    async def old_import(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        tier: Choice[str] | None = None,
    ) -> None:
        if not await self._owner(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        resolved = None if tier is None else LoyaltyTier(tier.value)
        if resolved is None:
            resolved = bound_tier(self.bot.config_by_guild.get(guild.id), role.id)
        if resolved is None:
            await respond(interaction, denied(copy.OWNER_OLD_NEED_TIER))
            return
        if not guild.chunked:
            await guild.chunk()
        members = sum(1 for member in role.members if not member.bot)
        if members <= 0:
            await respond(interaction, empty(copy.OWNER_MEMBERS_NONE))
            return
        actor_id = interaction.user.id

        async def yes(click: discord.Interaction) -> None:
            if not await owner_click(click, self._same_owner, actor_id):
                return
            await run_owner_progress(
                click,
                title=copy.OWNER_OLD_TITLE,
                registry=self.bot.motion,
                run=lambda on_progress: self.bot.ops.import_old(
                    guild, role=role, tier=resolved, on_progress=on_progress
                ),
                done=old_import_view,
                unit=copy.PROGRESS_UNIT_MEMBERS,
            )

        cancel = dismiss_owner(self._same_owner, actor_id, copy.OWNER_OLD_TITLE)
        await respond(
            interaction,
            old_import_confirm(role=role.mention, tier=resolved, members=members, on_yes=yes, on_no=cancel),
        )

    @app_commands.command(name="seed", description=copy.CMD_OWNER_SEED)
    @app_commands.describe(with_old=copy.CMD_OWNER_SEED_OLD, mode=copy.CMD_OWNER_SEED_MODE)
    @app_commands.choices(mode=SEED_MODE_CHOICES)
    async def seed(
        self, interaction: discord.Interaction, with_old: bool = False, mode: Choice[str] | None = None
    ) -> None:
        if not await self._owner(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        actor_id = interaction.user.id
        await run_owner_seed(
            interaction,
            guild=guild,
            seed=self.bot.seed,
            motion=self.bot.motion,
            with_old=with_old,
            mode=self._choice(mode),
            actor_id=actor_id,
            on_map=self.bot.setup_map.opener(actor_id=actor_id, with_old=with_old),
        )

    @app_commands.command(name="lift", description=copy.CMD_OWNER_LIFT)
    @app_commands.describe(user=copy.CMD_OWNER_LIFT_USER)
    async def lift(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self._owner(interaction) or not await ack(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        report = await self.bot.ops.lift_newcomer(guild, user, actor_id=interaction.user.id)
        await respond(
            interaction,
            lift_done(days=report.tenure_days, score=report.score, step=report.step, verified=report.verified),
        )

    @app_commands.command(name="rep", description=copy.CMD_OWNER_REP)
    @app_commands.describe(user=copy.CMD_OWNER_REP_USER, score=copy.CMD_OWNER_REP_SCORE)
    async def rep(
        self, interaction: discord.Interaction, user: discord.Member, score: app_commands.Range[int, 0, 100]
    ) -> None:
        if not await self._owner(interaction) or not await ack(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        before, after = await self.bot.reputation.set_score(guild.id, user.id, int(score), actor_id=interaction.user.id)
        await respond(interaction, rep_done(before=before, after=after))

    @app_commands.command(name="emojis", description=copy.CMD_OWNER_EMOJIS)
    async def emojis(self, interaction: discord.Interaction) -> None:
        if not await self._owner(interaction) or not await ack(interaction):
            return
        report = await deploy_emojis(self.bot)
        await show_emoji_deploy(interaction, report=report, attach=self.bot.attach_icons, now=time())

    @app_commands.command(name="limits", description=copy.CMD_OWNER_LIMITS)
    async def limits(self, interaction: discord.Interaction) -> None:
        if not await self._owner(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        moment = time()
        packed = snapshot_holds(self.bot, guild.id, now=moment)
        actor_id = interaction.user.id
        await run_owner_limits(
            interaction, packed=packed, now=moment, same_owner=self._same_owner, actor_id=actor_id,
            on_clear=lambda: _clear_create_holds(self.bot, guild.id, actor_id, len(packed)),
            observer=getattr(self.bot, "rate_observer", None),
        )

    @app_commands.command(name="export", description=copy.CMD_OWNER_EXPORT)
    @app_commands.describe(section=copy.CMD_OWNER_EXPORT_SECTION, with_docs=copy.CMD_OWNER_EXPORT_DOCS)
    @app_commands.choices(section=EXPORT_SECTION_CHOICES)
    async def export_cmd(
        self, interaction: discord.Interaction, section: Choice[str] | None = None, with_docs: bool = True
    ) -> None:
        if await self._owner(interaction):
            await run_blueprint_export(
                interaction, service=self.bot.blueprint, section=self._choice(section), with_docs=with_docs
            )

    @app_commands.command(name="apply", description=copy.CMD_OWNER_APPLY)
    @app_commands.describe(file=copy.CMD_OWNER_APPLY_FILE, mode=copy.CMD_OWNER_APPLY_MODE)
    @app_commands.choices(mode=APPLY_MODE_CHOICES)
    async def apply_cmd(
        self, interaction: discord.Interaction, file: discord.Attachment, mode: Choice[str] | None = None
    ) -> None:
        if await self._owner(interaction):
            await run_blueprint_apply(
                interaction, service=self.bot.blueprint, attachment=file,
                mode=self._choice(mode), same_owner=self._same_owner,
            )


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Owner(bot))


def _clear_create_holds(bot: Conductor, guild_id: int, actor_id: int, count: int) -> None:
    clear_guild(bot, guild_id, now=time())
    log_change(
        bot, guild_id, page="owner", field=copy.OWNER_LIMITS_FIELD, before=str(count), after="0", actor_id=actor_id
    )
