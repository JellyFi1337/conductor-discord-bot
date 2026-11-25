"""Состав следящих: список, кадры, смены, заметки."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import discord
import structlog
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

from conductor.db.session import session_scope
from conductor.domain.access import (
    WATCHERS_APPOINT,
    WATCHERS_DISMISS,
    WATCHERS_DUTY_OTHER,
    WATCHERS_NOTE,
    WATCHERS_READ,
    AccessSnapshot,
    Rank,
    decide,
    parse_rank_token,
    resolve_rank,
)
from conductor.domain.staff_feed import (
    ACTION_WATCH_APPOINT,
    ACTION_WATCH_DISMISS,
    ACTION_WATCH_DUTY,
    ACTION_WATCH_NOTE,
)
from conductor.presentation import copy
from conductor.presentation.access import allow_key, keyed
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, empty, error_code, failure, panel, respond
from conductor.presentation.tokens import Tone
from conductor.presentation.watchers import format_roster_lines, roster_card, watcher_card
from conductor.services.access import cached_snapshot
from conductor.services.pulse import bump_pulse
from conductor.services.role_broker import log_role_failure
from conductor.services.watchers import (
    add_note,
    build_roster,
    chunk_lines,
    load_policy,
    plan_appoint,
    plan_dismiss,
    set_duty,
)
from conductor.services.watchers_report import load_active_suspension

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.watchers")

_RANK_CHOICES = [
    app_commands.Choice(name=copy.RANK_WATCHER, value=Rank.WATCHER.key),
    app_commands.Choice(name=copy.RANK_SENIOR, value=Rank.SENIOR.key),
    app_commands.Choice(name=copy.RANK_CHIEF, value=Rank.CHIEF.key),
    app_commands.Choice(name=copy.RANK_SPECIAL, value=Rank.SPECIAL.key),
]


def _role_ids(member: discord.abc.User) -> tuple[int, ...]:
    return tuple(role.id for role in getattr(member, "roles", ()))


@app_commands.guild_only()
class Watchers(commands.GroupCog, group_name="watchers"):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    def _actor(self, interaction: discord.Interaction, snapshot: AccessSnapshot) -> Rank:
        return resolve_rank(_role_ids(interaction.user), snapshot.rank_roles)

    @app_commands.command(name="list", description=copy.CMD_WATCHERS_LIST)
    @app_commands.describe(user=copy.CMD_WATCHERS_INFO)
    @keyed(WATCHERS_READ)
    async def list_(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if user is not None:
            if not await ack(interaction):
                return
            await self._card(interaction, user)
            return
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        snapshot = await cached_snapshot(self.bot, guild.id)
        people = await build_roster(self.bot, guild, snapshot)
        lines = format_roster_lines(people)
        if not lines:
            await respond(interaction, empty(copy.WATCHERS_EMPTY, hint=copy.WATCHERS_EMPTY_HINT))
            return
        watched = await self.bot.moderation.watched_count(guild.id)
        title = f"{copy.watchers_count_title(len(people))} · {copy.watchers_watched(watched)}"
        await respond(interaction, roster_card(title, chunk_lines(lines), 1))

    async def _card(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if user.bot:
            await respond(interaction, denied(copy.WATCHERS_BOT))
            return
        guild = interaction.guild
        assert guild is not None
        snapshot = await cached_snapshot(self.bot, guild.id)
        payload = await self.bot.watchers.card_kwargs(guild, user, self._actor(interaction, snapshot))
        await respond(interaction, watcher_card(actor_id=interaction.user.id, **payload))

    @app_commands.command(name="appoint", description=copy.CMD_WATCHERS_APPOINT)
    @app_commands.describe(user=copy.CMD_WATCHERS_APPOINT_USER, rank=copy.CMD_WATCHERS_APPOINT_RANK)
    @app_commands.choices(rank=_RANK_CHOICES)
    async def appoint(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rank: Choice[str],
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        snapshot = await cached_snapshot(self.bot, guild.id)
        async with session_scope(self.bot.sessions) as session:
            policy = await load_policy(session, guild.id)
            # Чистый `decide` ниже про отстранение не знает — отстранённый чиф не назначает.
            suspension = (
                None
                if self.bot.is_developer(interaction.user.id)
                else await load_active_suspension(session, guild.id, interaction.user.id)
            )
        if suspension is not None:
            await respond(
                interaction,
                denied(copy.watchers_suspended_deny(until=suspension.until, reason=suspension.reason)),
            )
            return
        actor = self._actor(interaction, snapshot)
        decision = decide(
            snapshot,
            key=WATCHERS_APPOINT,
            member_role_ids=_role_ids(interaction.user),
            is_administrator=bool(getattr(interaction.user.guild_permissions, "administrator", False)),
            is_developer=self.bot.is_developer(interaction.user.id),
        )
        desired = parse_rank_token(rank.value)
        plan = plan_appoint(
            snapshot=snapshot,
            policy=policy,
            actor=actor,
            decision=decision,
            target_role_ids=_role_ids(user),
            target_is_bot=user.bot,
            desired=desired,
        )
        if plan.need_key:
            await allow_key(interaction, WATCHERS_APPOINT)
            return
        if plan.block is not None:
            hint = copy.WATCHERS_UNBOUND_HINT if plan.unbound else None
            await respond(interaction, denied(copy.watchers_block(plan.block.value), need=hint))
            return
        extra = copy.WATCHERS_BASIS_SETTING if plan.senior_basis else None
        await self._apply_rank(
            interaction,
            user,
            add=plan.add,
            remove=plan.remove,
            title=copy.WATCHERS_APPOINTED,
            action=copy.watchers_appoint_action(desired.key),
            basis=copy.watchers_basis(rank=actor.key, key=WATCHERS_APPOINT, extra=extra),
            tone=Tone.OK,
            actor_rank=actor,
            feed_action=ACTION_WATCH_APPOINT,
        )

    @app_commands.command(name="dismiss", description=copy.CMD_WATCHERS_DISMISS)
    @keyed(WATCHERS_DISMISS)
    async def dismiss(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        snapshot = await cached_snapshot(self.bot, guild.id)
        actor = self._actor(interaction, snapshot)
        plan = plan_dismiss(
            snapshot=snapshot,
            actor=actor,
            target_role_ids=_role_ids(user),
            target_is_bot=user.bot,
            is_developer=self.bot.is_developer(interaction.user.id),
        )
        if plan.block is not None:
            await respond(interaction, denied(copy.watchers_block(plan.block.value)))
            return
        if not await self._apply_rank(
            interaction,
            user,
            add=plan.add,
            remove=plan.remove,
            title=copy.WATCHERS_DISMISSED,
            action=copy.watchers_dismiss_action(plan.current.key),
            basis=copy.watchers_basis(rank=actor.key, key=WATCHERS_DISMISS),
            tone=Tone.WARN,
            actor_rank=actor,
            feed_action=ACTION_WATCH_DISMISS,
        ):
            return
        async with session_scope(self.bot.sessions) as session:
            await set_duty(session, guild.id, user.id, False, actor_id=interaction.user.id, now=datetime.now(UTC))
        bump_pulse(self.bot, guild.id)

    @app_commands.command(name="duty", description=copy.CMD_WATCHERS_DUTY)
    @app_commands.describe(state=copy.CMD_WATCHERS_DUTY_STATE, user=copy.CMD_WATCHERS_DUTY_USER)
    async def duty(
        self,
        interaction: discord.Interaction,
        state: Literal["on", "off"],
        user: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        target = user or interaction.user
        if not isinstance(target, discord.Member):
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        if target.bot:
            await respond(interaction, denied(copy.WATCHERS_BOT))
            return
        if not await ack(interaction):
            return
        snapshot = await cached_snapshot(self.bot, guild.id)
        actor = self._actor(interaction, snapshot)
        other = target.id != interaction.user.id
        if other:
            if not await allow_key(interaction, WATCHERS_DUTY_OTHER):
                return
        elif actor < Rank.WATCHER:
            await respond(interaction, denied(copy.WATCHERS_DUTY_SELF, need=copy.RANK_WATCHER))
            return
        elif await self.bot.watchers_report.is_suspended(guild.id, interaction.user.id):
            await respond(interaction, denied(copy.WATCHERS_DUTY_SUSPENDED))
            return
        if resolve_rank(_role_ids(target), snapshot.rank_roles) is Rank.NONE:
            await respond(interaction, denied(copy.WATCHERS_DUTY_TARGET))
            return
        on = state == "on"
        if on and await self.bot.watchers_report.is_suspended(guild.id, target.id):
            await respond(interaction, denied(copy.WATCHERS_DUTY_SUSPENDED))
            return
        async with session_scope(self.bot.sessions) as session:
            changed = await set_duty(
                session,
                guild.id,
                target.id,
                on,
                actor_id=interaction.user.id,
                now=datetime.now(UTC),
            )
        if changed:
            bump_pulse(self.bot, guild.id)
        extra = None if other else copy.WATCHERS_BASIS_SELF
        key = WATCHERS_DUTY_OTHER if other else None
        if changed:
            await self.bot.watchers.record_staff(
                guild.id,
                copy.watchers_staff_action(
                    who=target.mention,
                    action=copy.watchers_duty_action(on),
                    actor=interaction.user.mention,
                    basis=copy.watchers_basis(rank=actor.key, key=key, extra=extra),
                ),
                actor_id=interaction.user.id,
                actor_rank=actor,
                action=ACTION_WATCH_DUTY,
                target=target.id,
                details=copy.watchers_duty_action(on),
            )
        await respond(interaction, panel(copy.WATCHERS_DUTY_SET, copy.watchers_duty_action(on), tone=Tone.OK))

    @app_commands.command(name="note", description=copy.CMD_WATCHERS_NOTE)
    @keyed(WATCHERS_NOTE)
    async def note(self, interaction: discord.Interaction, user: discord.Member, text: str) -> None:
        if user.bot:
            await respond(interaction, denied(copy.WATCHERS_BOT))
            return
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return
        snapshot = await cached_snapshot(self.bot, guild.id)
        actor = self._actor(interaction, snapshot)
        async with session_scope(self.bot.sessions) as session:
            stored = await add_note(
                session,
                guild.id,
                user.id,
                author_id=interaction.user.id,
                body=text,
                now=datetime.now(UTC),
            )
        await self.bot.watchers.record_staff(
            guild.id,
            copy.watchers_staff_action(
                who=user.mention,
                action=stored.body,
                actor=interaction.user.mention,
                basis=copy.watchers_basis(rank=actor.key, key=WATCHERS_NOTE),
            ),
            actor_id=interaction.user.id,
            actor_rank=actor,
            action=ACTION_WATCH_NOTE,
            target=user.id,
            details=stored.body,
        )
        await respond(interaction, panel(copy.WATCHERS_NOTE_SAVED, stored.body, tone=Tone.OK))

    async def _apply_rank(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        *,
        add: tuple[int, ...],
        remove: tuple[int, ...],
        title: str,
        action: str,
        basis: str,
        tone: Tone,
        actor_rank: Rank,
        feed_action: str,
    ) -> bool:
        guild = interaction.guild
        assert guild is not None
        if not await ack(interaction):
            return False
        result = await self.bot.watchers.apply_rank(
            guild_id=guild.id,
            user_id=user.id,
            add=add,
            remove=remove,
            actor_id=interaction.user.id,
            reason=copy.watchers_reason(action),
        )
        if not result.ok:
            log_role_failure(log, "staff_role_failed", guild_id=guild.id, user_id=user.id, result=result)
            await respond(interaction, failure(error_code(interaction)))
            return False
        await self.bot.watchers.record_staff(
            guild.id,
            copy.watchers_staff_action(
                who=user.mention,
                action=action,
                actor=interaction.user.mention,
                basis=basis,
            ),
            actor_id=interaction.user.id,
            actor_rank=actor_rank,
            action=feed_action,
            target=user.id,
            details=action,
            tone=tone,
        )
        await respond(interaction, panel(title, action, tone=tone))
        return True

    def notify_special_locked(self, guild: discord.Guild) -> None:
        self.bot.watchers.notify_special_locked(guild)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            self.bot.spawn(self.bot.watchers.scan_unofficial(guild), name=f"watchers-scan-{guild.id}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self.bot.watchers.maybe_unofficial(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot:
            return
        snapshot = await cached_snapshot(self.bot, after.guild.id)
        before_rank = resolve_rank((role.id for role in before.roles), snapshot.rank_roles)
        after_rank = resolve_rank((role.id for role in after.roles), snapshot.rank_roles)
        skip_log = self.bot.watchers.consume_expected(after.guild.id, after.id)
        if before_rank is not after_rank and not skip_log:
            self.bot.watchers.log_rank_change(after, before_rank, after_rank)
        if after_rank is Rank.NONE:
            await self.bot.watchers.maybe_unofficial(after)
        else:
            await self.bot.watchers.clear_member_unofficial(after.guild.id, after.id)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Watchers(bot))
