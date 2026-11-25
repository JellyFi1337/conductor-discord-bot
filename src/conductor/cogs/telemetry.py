from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
import structlog
from discord.ext import commands, tasks

from conductor.db.session import session_scope
from conductor.domain.activity import EventKind
from conductor.domain.history import emoji_key
from conductor.services.census import mark_left, upsert_member
from conductor.services.history import facts_from_message, skip_ids_from_config
from conductor.services.pulse import bump_pulse

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.telemetry")


class Telemetry(commands.Cog):
    """Silent collectors: census mutations, activity buckets, voice sessions."""

    def __init__(self, bot: Conductor) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.flush_activity.start()

    async def cog_unload(self) -> None:
        self.flush_activity.cancel()
        await self.bot.collector.flush(self.bot.sessions)
        await self.bot.history.flush(self.bot.sessions)

    @tasks.loop(seconds=30)
    async def flush_activity(self) -> None:
        await self.bot.collector.flush(self.bot.sessions)
        if self.bot.history_busy:
            return
        await self.bot.history.flush(self.bot.sessions)

    @flush_activity.before_loop
    async def before_flush(self) -> None:
        await self.bot.wait_until_ready()

    @flush_activity.error
    async def flush_activity_error(self, error: BaseException) -> None:
        log.exception("activity_flush_failed", error=repr(error))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        joined = member.joined_at or datetime.now(UTC)
        invite = await self.bot.invites.consume_join(member.guild)
        self.bot.last_used_invite[member.id] = invite
        async with session_scope(self.bot.sessions) as session:
            await upsert_member(
                session,
                guild_id=member.guild.id,
                user_id=member.id,
                username=str(member),
                display_name=member.display_name,
                is_bot=member.bot,
                joined_at=joined,
                account_created_at=member.created_at,
                booster_since=member.premium_since,
                invite_code=await self._invite_code(invite),
                seen_at=joined,
            )
        if not member.bot:
            self.bot.collector.bump(member.guild.id, joined, EventKind.JOIN, user_id=member.id)
            bump_pulse(self.bot, member.guild.id, now=joined)
        caches = getattr(self.bot, "caches", None)
        drop = getattr(caches, "invalidate_joined", None)
        if callable(drop):
            drop(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        now = datetime.now(UTC)
        async with session_scope(self.bot.sessions) as session:
            await mark_left(session, guild_id=member.guild.id, user_id=member.id, left_at=now)
        if not member.bot:
            self.bot.collector.bump(member.guild.id, now, EventKind.LEAVE, user_id=member.id)
            bump_pulse(self.bot, member.guild.id, now=now)
        await self.bot.voice_sessions.close_member(
            self.bot.sessions,
            guild_id=member.guild.id,
            user_id=member.id,
            now=now,
        )
        self.bot.forget_voice(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if self._log_channel(message.guild.id, message.channel.id):
            return
        if message.guild.id in self.bot.history_busy:
            return
        if self.bot.history.known_message(message.guild.id, message.id):
            return
        self.bot.collector.bump(
            message.guild.id,
            message.created_at,
            EventKind.MESSAGE,
            channel_id=message.channel.id,
            user_id=message.author.id,
        )
        if self.bot.history.accept_live(message.guild.id, message.channel.id, message.id):
            facts = facts_from_message(message)
            if facts is not None:
                self.bot.history.apply(facts)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None:
            return
        author = (payload.data.get("author") or {}).get("id")
        user_id = int(author) if author else None
        self.bot.collector.bump(
            payload.guild_id,
            datetime.now(UTC),
            EventKind.MESSAGE_EDIT,
            channel_id=payload.channel_id,
            user_id=user_id,
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        self.bot.collector.bump(
            payload.guild_id,
            datetime.now(UTC),
            EventKind.MESSAGE_DELETE,
            channel_id=payload.channel_id,
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return
        if self._log_channel(payload.guild_id, payload.channel_id):
            return
        self.bot.collector.bump(
            payload.guild_id,
            datetime.now(UTC),
            EventKind.REACTION,
            channel_id=payload.channel_id,
            user_id=payload.user_id,
        )
        if payload.guild_id in self.bot.history_busy:
            return
        if not self.bot.history.snapshot_window(payload.guild_id, payload.channel_id):
            return
        cached = self.bot.messages.get(payload.message_id)
        if cached is None or cached.author_id == payload.user_id:
            return
        self.bot.history.apply_reaction(
            payload.guild_id,
            payload.user_id,
            cached.author_id,
            datetime.now(UTC),
            message_id=payload.message_id,
            emoji=emoji_key(payload.emoji),
        )

    def _log_channel(self, guild_id: int, channel_id: int) -> bool:
        config = self.bot.config_by_guild.get(guild_id)
        return channel_id in skip_ids_from_config(config)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            # Музыкальные боты сидят в голосе сутками — это не активность участников.
            return
        now = datetime.now(UTC)
        before_id = None if before.channel is None else before.channel.id
        after_id = None if after.channel is None else after.channel.id
        await self.bot.voice_sessions.apply(
            self.bot.sessions,
            guild_id=member.guild.id,
            user_id=member.id,
            before_channel_id=before_id,
            after_channel_id=after_id,
            now=now,
        )
        if before_id != after_id:
            bump_pulse(self.bot, member.guild.id, now=now)
        if before.channel is None and after.channel is not None:
            self.bot.collector.bump(
                member.guild.id,
                now,
                EventKind.VOICE_JOIN,
                channel_id=after.channel.id,
                user_id=member.id,
            )

    async def _invite_code(self, invite: object | None) -> str | None:
        code = getattr(invite, "code", None)
        if not code:
            return None
        return str(code)


async def setup(bot: Conductor) -> None:
    await bot.add_cog(Telemetry(bot))
