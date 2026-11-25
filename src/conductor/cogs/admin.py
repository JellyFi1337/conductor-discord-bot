"""Панель настройки сервера: /admin panel и /admin import."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from conductor.db.session import session_scope
from conductor.domain.access import ADMIN_PANEL_OPEN, CONFIG_MANAGE
from conductor.domain.admin import AdminCtx
from conductor.presentation import copy
from conductor.presentation.access import keyed
from conductor.presentation.admin import no_pages_card
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, panel, respond
from conductor.presentation.tokens import Tone
from conductor.services.access import example_permissions_path, import_yaml
from conductor.services.admin_pages import log_change
from conductor.services.admin_panel import open_panel
from conductor.services.watchers_report import cached_active_suspension

if TYPE_CHECKING:
    from conductor.bot import Conductor


async def _read_import_text(attachment: discord.Attachment | None) -> str | None:
    if attachment is not None:
        return (await attachment.read()).decode("utf-8")
    path = example_permissions_path()
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


@app_commands.guild_only()
class Admin(commands.GroupCog, group_name="admin"):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    async def _ctx(self, interaction: discord.Interaction) -> AdminCtx:
        guild = interaction.guild
        user = interaction.user
        assert guild is not None
        found = await cached_active_suspension(self.bot, guild.id, user.id)
        return AdminCtx(
            bot=self.bot,
            guild_id=guild.id,
            actor_id=user.id,
            guild=guild,
            member_role_ids=tuple(role.id for role in getattr(user, "roles", ())),
            is_administrator=bool(getattr(getattr(user, "guild_permissions", None), "administrator", False)),
            is_developer=self.bot.is_developer(user.id),
            suspended=found is not None,
        )

    @app_commands.command(name="panel", description=copy.CMD_ADMIN_PANEL)
    @keyed(ADMIN_PANEL_OPEN)
    async def panel_cmd(self, interaction: discord.Interaction) -> None:
        view = await open_panel(await self._ctx(interaction))
        if view is None:
            await respond(interaction, no_pages_card())
            return
        await respond(interaction, view)

    @app_commands.command(name="import", description=copy.CMD_ADMIN_IMPORT)
    @app_commands.describe(file=copy.CMD_ADMIN_IMPORT_FILE)
    @keyed(CONFIG_MANAGE)
    async def import_(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment | None = None,
    ) -> None:
        if not await ack(interaction):
            return
        assert interaction.guild is not None
        raw = await _read_import_text(file)
        if raw is None:
            await respond(interaction, denied(copy.CONFIG_NO_FILE, need=copy.RANK_SPECIAL))
            return
        async with session_scope(self.bot.sessions) as session:
            snapshot = await import_yaml(session, interaction.guild.id, raw)
        self.bot.caches.invalidate_access(interaction.guild.id)
        if snapshot.special_locked:
            notify = getattr(self.bot.watchers, "notify_special_locked", None)
            if callable(notify):
                notify(interaction.guild)
        log_change(
            self.bot,
            interaction.guild.id,
            page=copy.ADMIN_PAGE_PERMS,
            field=copy.ADMIN_FIELD_PERMS,
            before="—",
            after=copy.CONFIG_IMPORTED,
            actor_id=interaction.user.id,
        )
        footnote = copy.CONFIG_FALLBACK if not snapshot.special_locked else None
        await respond(
            interaction,
            panel(copy.CONFIG_TITLE, copy.CONFIG_IMPORTED, tone=Tone.OK, footnote=footnote),
        )


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Admin(bot))
