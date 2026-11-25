from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from conductor.presentation import copy
from conductor.presentation.events import (
    bulk_delete_card,
    bulk_recovered,
    cache_from_edit,
    cache_from_message,
    channel_changes,
    channel_create_card,
    channel_delete_card,
    channel_update_card,
    delete_card,
    edit_card,
    emoji_name_diff,
    emoji_update_card,
    guild_changes,
    guild_channel,
    guild_update_card,
    invite_create_card,
    invite_delete_card,
    is_bot_message_edit,
    join_card,
    labeled_channel,
    leave_card,
    object_guild_id,
    profile_card,
    profile_changes,
    reaction_add_card,
    reaction_clear_card,
    reaction_clear_emoji_card,
    reaction_is_bot,
    reaction_jump,
    reaction_remove_card,
    scheduled_event_create_card,
    scheduled_event_delete_card,
    skip_log_channel,
    stage_create_card,
    stage_delete_card,
    stayed_label,
    sticker_name_diff,
    sticker_update_card,
    thread_changes,
    thread_create_card,
    thread_delete_card,
    thread_update_card,
    voice_card,
    voice_flag_changes,
    webhooks_card,
)
from conductor.presentation.text import change_line, channel_label
from conductor.presentation.tokens import Tone

if TYPE_CHECKING:
    from conductor.bot import Conductor


class UserLogs(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot
        self._voice_seen: dict[tuple[int, int], datetime] = {}

    def _user_channel(self, guild_id: int) -> int | None:
        config = self.bot.config_by_guild.get(guild_id)
        return None if config is None else config.user_log_channel_id

    def _send(self, guild_id: int, view: discord.ui.LayoutView) -> None:
        self.bot.log_pump.submit(self._user_channel(guild_id), view=view)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        cached = cache_from_message(message)
        if cached:
            self.bot.messages.store(cached)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None:
            return
        if skip_log_channel(self.bot, payload.guild_id, payload.channel_id):
            return
        cached = self.bot.messages.get(payload.message_id)
        if is_bot_message_edit(payload.data, cached):
            return
        after = payload.data.get("content")
        if after is None:
            return
        before = cached.content if cached else None
        if before == after:
            return
        author = payload.data.get("author") or {}
        author_id = int(author["id"]) if author.get("id") else (cached.author_id if cached else None)
        author_name = str(author.get("username") or (cached.author_name if cached else "?"))
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        snapshot = cache_from_edit(
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            author_id=author_id,
            author_name=author_name,
            content=str(after),
            jump=cached.jump_url or jump if cached else jump,
            cached=cached,
        )
        if snapshot is not None:
            self.bot.messages.store(snapshot)
        self._send(
            payload.guild_id,
            edit_card(
                channel=guild_channel(self.bot, payload.guild_id, payload.channel_id),
                channel_id=payload.channel_id,
                author_id=author_id,
                author_name=author_name,
                before=before,
                after=str(after),
                jump=jump,
            ),
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        if skip_log_channel(self.bot, payload.guild_id, payload.channel_id):
            return
        cached = self.bot.messages.pop(payload.message_id)
        if cached is None and payload.cached_message is not None:
            cached = cache_from_message(payload.cached_message)
        if cached is not None and cached.is_bot:
            return
        self._send(
            payload.guild_id,
            delete_card(
                cached,
                payload,
                channel=guild_channel(self.bot, payload.guild_id, payload.channel_id),
            ),
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        if skip_log_channel(self.bot, payload.guild_id, payload.channel_id):
            return
        found = []
        for message_id in payload.message_ids:
            cached = self.bot.messages.pop(message_id)
            if cached is not None:
                found.append(cached)
        recovered = bulk_recovered(found)
        self._send(
            payload.guild_id,
            bulk_delete_card(
                channel=guild_channel(self.bot, payload.guild_id, payload.channel_id),
                channel_id=payload.channel_id,
                deleted=len(payload.message_ids),
                recovered=recovered,
            ),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        invite = self.bot.last_used_invite.pop(member.id, None)
        self._send(member.guild.id, join_card(member, invite))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self._voice_seen.pop((member.guild.id, member.id), None)
        self._send(member.guild.id, leave_card(member))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        changes = profile_changes(before, after)
        if not changes:
            return
        thumbnail = after.display_avatar.url if after.display_avatar else None
        self._send(after.guild.id, profile_card(after, changes, thumbnail_url=thumbnail))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        key = (member.guild.id, member.id)
        now = datetime.now(UTC)
        lines: list[str] = []
        title = copy.LOG_VOICE
        tone = Tone.MUTED
        stayed: str | None = None
        if before.channel is None and after.channel is not None:
            title = copy.LOG_VOICE_JOIN
            lines.append(channel_label(after.channel))
            tone = Tone.OK
            self._voice_seen[key] = now
        elif before.channel is not None and after.channel is None:
            started = self._voice_seen.pop(key, None)
            stayed = stayed_label(started, now)
            title = copy.LOG_VOICE_LEAVE
            lines.append(channel_label(before.channel))
            tone = Tone.DANGER
        elif before.channel and after.channel and before.channel.id != after.channel.id:
            started = self._voice_seen.get(key)
            stayed = stayed_label(started, now)
            title = copy.LOG_VOICE_MOVE
            lines.append(change_line(channel_label(before.channel), channel_label(after.channel)))
            tone = Tone.WARN
            self._voice_seen[key] = now
        lines.extend(voice_flag_changes(before, after))
        if not lines:
            return
        self._send(member.guild.id, voice_card(member, title=title, lines=lines, tone=tone, stayed=stayed))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return
        if skip_log_channel(self.bot, payload.guild_id, payload.channel_id):
            return
        self._send(
            payload.guild_id,
            reaction_add_card(
                member=payload.member,
                emoji=str(payload.emoji),
                channel=labeled_channel(self.bot, payload.guild_id, payload.channel_id),
                jump=reaction_jump(payload.guild_id, payload.channel_id, payload.message_id),
            ),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        if reaction_is_bot(self.bot, payload):
            return
        if skip_log_channel(self.bot, payload.guild_id, payload.channel_id):
            return
        self._send(
            payload.guild_id,
            reaction_remove_card(
                user_id=payload.user_id,
                emoji=str(payload.emoji),
                channel=labeled_channel(self.bot, payload.guild_id, payload.channel_id),
                jump=reaction_jump(payload.guild_id, payload.channel_id, payload.message_id),
            ),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        if payload.guild_id is None:
            return
        if skip_log_channel(self.bot, payload.guild_id, payload.channel_id):
            return
        self._send(
            payload.guild_id,
            reaction_clear_card(
                channel=labeled_channel(self.bot, payload.guild_id, payload.channel_id),
                jump=reaction_jump(payload.guild_id, payload.channel_id, payload.message_id),
            ),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        if payload.guild_id is None:
            return
        if skip_log_channel(self.bot, payload.guild_id, payload.channel_id):
            return
        self._send(
            payload.guild_id,
            reaction_clear_emoji_card(
                emoji=str(payload.emoji),
                jump=reaction_jump(payload.guild_id, payload.channel_id, payload.message_id),
            ),
        )

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        await self.bot.invites.refresh(invite.guild)
        self._send(invite.guild.id, invite_create_card(invite))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        await self.bot.invites.refresh(invite.guild)
        self._send(invite.guild.id, invite_delete_card(invite))

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        self._send(thread.guild.id, thread_create_card(thread))

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        self._send(thread.guild.id, thread_delete_card(thread))

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        bits = thread_changes(before, after)
        if not bits:
            return
        self._send(after.guild.id, thread_update_card(after, bits))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        self._send(channel.guild.id, channel_create_card(channel))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self._send(channel.guild.id, channel_delete_card(channel))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        bits = channel_changes(before, after)
        if not bits:
            return
        self._send(after.guild.id, channel_update_card(after, bits))

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, scheduled: discord.ScheduledEvent) -> None:
        guild_id = object_guild_id(scheduled)
        if guild_id is None:
            return
        self._send(guild_id, scheduled_event_create_card(scheduled))

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, scheduled: discord.ScheduledEvent) -> None:
        guild_id = object_guild_id(scheduled)
        if guild_id is None:
            return
        self._send(guild_id, scheduled_event_delete_card(scheduled))

    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage: discord.StageInstance) -> None:
        guild_id = object_guild_id(stage)
        if guild_id is None:
            return
        self._send(guild_id, stage_create_card(stage))

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage: discord.StageInstance) -> None:
        guild_id = object_guild_id(stage)
        if guild_id is None:
            return
        self._send(guild_id, stage_delete_card(stage))

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self,
        guild: discord.Guild,
        before: tuple[discord.Emoji, ...],
        after: tuple[discord.Emoji, ...],
    ) -> None:
        added, removed = emoji_name_diff(before, after)
        if not added and not removed:
            return
        self._send(guild.id, emoji_update_card(added=added, removed=removed))

    @commands.Cog.listener()
    async def on_guild_stickers_update(
        self,
        guild: discord.Guild,
        before: tuple[discord.GuildSticker, ...],
        after: tuple[discord.GuildSticker, ...],
    ) -> None:
        added, removed = sticker_name_diff(before, after)
        if not added and not removed:
            return
        self._send(guild.id, sticker_update_card(added=added, removed=removed))

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        bits = guild_changes(before, after)
        if not bits:
            return
        self._send(after.id, guild_update_card(bits))

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        self._send(channel.guild.id, webhooks_card(channel))


async def setup(bot: Conductor) -> None:
    await bot.add_cog(UserLogs(bot))
