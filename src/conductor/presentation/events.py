from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import discord
from discord import ui

from conductor.domain.insights import age_label
from conductor.domain.records import CachedMessage, UsedInvite
from conductor.presentation import copy
from conductor.presentation.layout import event
from conductor.presentation.text import (
    FOOTER_LOYALTY,
    FOOTER_USER,
    change_line,
    channel_label,
    channel_type_label,
    duration_label,
    jump_link,
    quote_block,
    stamp,
    stamp_pair,
    truncate,
    user_label,
)
from conductor.presentation.tokens import Tone

__all__ = [
    "bulk_delete_card",
    "bulk_recovered",
    "cache_from_edit",
    "cache_from_message",
    "channel_changes",
    "channel_create_card",
    "channel_delete_card",
    "channel_update_card",
    "delete_card",
    "duration_label",
    "edit_card",
    "emoji_name_diff",
    "emoji_update_card",
    "guild_changes",
    "guild_update_card",
    "invite_create_card",
    "invite_delete_card",
    "invite_ttl",
    "is_bot_message_edit",
    "guild_channel",
    "labeled_channel",
    "join_card",
    "leave_card",
    "loyalty_revert_card",
    "object_guild_id",
    "profile_card",
    "profile_changes",
    "reaction_add_card",
    "reaction_clear_card",
    "reaction_clear_emoji_card",
    "reaction_is_bot",
    "reaction_jump",
    "reaction_remove_card",
    "scheduled_event_create_card",
    "scheduled_event_delete_card",
    "skip_log_channel",
    "stage_create_card",
    "stage_delete_card",
    "stayed_label",
    "sticker_name_diff",
    "sticker_update_card",
    "thread_changes",
    "thread_create_card",
    "thread_delete_card",
    "thread_update_card",
    "voice_card",
    "voice_flag_changes",
    "webhooks_card",
]


def cache_from_edit(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_id: int | None,
    author_name: str,
    content: str,
    jump: str,
    cached: CachedMessage | None,
) -> CachedMessage | None:
    """Снимок после залогированной правки: следующий delete не пишет «нет в кэше»."""
    if author_id is None:
        return None
    created = cached.created_at if cached is not None else discord.utils.snowflake_time(int(message_id))
    return CachedMessage(
        id=int(message_id),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        author_id=int(author_id),
        author_name=cached.author_name if cached is not None else author_name,
        content=content,
        created_at=created,
        attachments=() if cached is None else cached.attachments,
        jump_url=jump,
        stickers=() if cached is None else cached.stickers,
        reply_id=None if cached is None else cached.reply_id,
        is_bot=False,
    )


def cache_from_message(message: discord.Message) -> CachedMessage | None:
    if message.guild is None:
        return None
    reply_id = message.reference.message_id if message.reference else None
    return CachedMessage(
        id=message.id,
        guild_id=message.guild.id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        author_name=str(message.author),
        content=message.content,
        created_at=message.created_at,
        attachments=tuple(attachment.filename or attachment.url for attachment in message.attachments),
        jump_url=message.jump_url,
        stickers=tuple(sticker.name for sticker in message.stickers),
        reply_id=reply_id,
        is_bot=message.author.bot,
    )


def is_bot_message_edit(data: Mapping[str, object], cached: CachedMessage | None) -> bool:
    """Не логировать правку, пока нельзя подтвердить, что автор — человек.

    Discord часто опускает `author` у правок ботов и вебхуков. Пустой словарь
    раньше проходил проверку `author.get("bot")` как «не бот».
    """
    author = data.get("author")
    if isinstance(author, Mapping) and author:
        if author.get("bot") is True:
            return True
        if author.get("bot") is False:
            return False
        if author.get("id"):
            return False
    if data.get("webhook_id"):
        return True
    if cached is not None:
        return cached.is_bot
    return True


def skip_log_channel(bot: object, guild_id: int, channel_id: int | None) -> bool:
    if channel_id is None:
        return False
    config = getattr(bot, "config_by_guild", {}).get(int(guild_id))
    if config is not None and channel_id in {config.user_log_channel_id, config.admin_log_channel_id}:
        return True
    probe = getattr(getattr(bot, "verification", None), "channel_id", None)
    return callable(probe) and probe(int(guild_id)) == int(channel_id)


def guild_channel(bot: object, guild_id: int, channel_id: int) -> object:
    guild = getattr(bot, "get_guild", lambda _id: None)(guild_id)
    if guild is None:
        return None
    return guild.get_channel(channel_id) or guild.get_thread(channel_id)


def labeled_channel(bot: object, guild_id: int, channel_id: int) -> str:
    return channel_label(guild_channel(bot, guild_id, channel_id), channel_id)


def object_guild_id(obj: object) -> int | None:
    raw = getattr(obj, "guild_id", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def stayed_label(started: datetime | None, now: datetime) -> str:
    if started is None:
        return copy.LOG_JOIN_UNKNOWN
    return duration_label(int((now - started).total_seconds()))


def profile_changes(before: discord.Member, after: discord.Member) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    if before.nick != after.nick:
        changes.append((copy.LOG_NICK, change_line(before.nick or before.name, after.nick or after.name)))
    if before.display_avatar.url != after.display_avatar.url:
        changes.append((copy.LOG_AVATAR, copy.LOG_AVATAR_UPDATED))
    if before.pending != after.pending and not after.pending:
        changes.append((copy.LOG_RULES, copy.LOG_RULES_OK))
    if before.premium_since != after.premium_since:
        old_boost = stamp(before.premium_since, "D") if before.premium_since else copy.NO
        new_boost = stamp(after.premium_since, "D") if after.premium_since else copy.LOG_BOOST_OFF
        changes.append((copy.LOG_BOOST, change_line(old_boost, new_boost)))
    return changes


def thread_changes(before: discord.Thread, after: discord.Thread) -> list[str]:
    bits: list[str] = []
    if before.name != after.name:
        bits.append(copy.log_named(copy.LOG_NAME, change_line(before.name, after.name)))
    if before.archived != after.archived:
        bits.append(copy.log_archived(after.archived))
    if before.locked != after.locked:
        bits.append(copy.log_locked(after.locked))
    return bits


def channel_changes(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> list[str]:
    bits: list[str] = []
    if before.name != after.name:
        bits.append(copy.log_named(copy.LOG_NAME, change_line(before.name, after.name)))
    if getattr(before, "topic", None) != getattr(after, "topic", None):
        bits.append(copy.LOG_TOPIC)
    before_nsfw = getattr(before, "nsfw", None)
    after_nsfw = getattr(after, "nsfw", None)
    if before_nsfw != after_nsfw:
        bits.append(copy.log_nsfw(bool(after_nsfw)))
    if before.overwrites != after.overwrites:
        bits.append(copy.LOG_OVERWRITES)
    return bits


def guild_changes(before: discord.Guild, after: discord.Guild) -> list[str]:
    bits: list[str] = []
    if before.name != after.name:
        bits.append(copy.log_named(copy.LOG_NAME, change_line(before.name, after.name)))
    if before.icon != after.icon:
        bits.append(copy.LOG_ICON)
    if before.vanity_url_code != after.vanity_url_code:
        bits.append(copy.log_named(copy.LOG_VANITY, change_line(before.vanity_url_code, after.vanity_url_code)))
    if before.premium_tier != after.premium_tier:
        bits.append(copy.log_named(copy.LOG_PREMIUM, change_line(before.premium_tier, after.premium_tier)))
    return bits


def emoji_name_diff(
    before: tuple[discord.Emoji, ...],
    after: tuple[discord.Emoji, ...],
) -> tuple[list[str], list[str]]:
    before_ids = {emoji.id: emoji for emoji in before}
    after_ids = {emoji.id: emoji for emoji in after}
    added = [emoji.name for emoji_id, emoji in after_ids.items() if emoji_id not in before_ids]
    removed = [emoji.name for emoji_id, emoji in before_ids.items() if emoji_id not in after_ids]
    return added, removed


def sticker_name_diff(
    before: tuple[discord.GuildSticker, ...],
    after: tuple[discord.GuildSticker, ...],
) -> tuple[list[str], list[str]]:
    before_ids = {item.id for item in before}
    after_ids = {item.id for item in after}
    added = [item.name for item in after if item.id not in before_ids]
    removed = [item.name for item in before if item.id not in after_ids]
    return added, removed


def bulk_recovered(messages: list[CachedMessage]) -> list[str]:
    recovered: list[str] = []
    for cached in messages:
        snippet = (cached.content or "").replace("\n", " ").strip() or copy.LOG_EMPTY
        recovered.append(f"**{cached.author_name}:** {truncate(snippet, 80)}")
    return recovered


def reaction_jump(guild_id: int, channel_id: int, message_id: int) -> str:
    return jump_link(f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}")


def reaction_is_bot(bot: object, payload: discord.RawReactionActionEvent) -> bool:
    member = payload.member
    if member is not None:
        return bool(member.bot)
    me = getattr(bot, "user", None)
    if me is not None and payload.user_id == me.id:
        return True
    guild = getattr(bot, "get_guild", lambda _id: None)(payload.guild_id) if payload.guild_id else None
    found = None if guild is None else guild.get_member(payload.user_id)
    return bool(found is not None and found.bot)


def voice_flag_changes(before: discord.VoiceState, after: discord.VoiceState) -> list[str]:
    mapping = (
        ("mute", "серверный мут", "вкл", "выкл"),
        ("deaf", "серверная глухота", "вкл", "выкл"),
        ("self_mute", "микрофон", "выкл", "вкл"),
        ("self_deaf", "наушники", "выкл", "вкл"),
        ("self_stream", "стрим", "вкл", "выкл"),
        ("self_video", "камера", "вкл", "выкл"),
        ("suppress", "глушение на трибуне", "вкл", "выкл"),
    )
    changes: list[str] = []
    for attr, label, on_text, off_text in mapping:
        old = getattr(before, attr)
        new = getattr(after, attr)
        if old == new:
            continue
        changes.append(f"{label}: {on_text if new else off_text}")
    return changes


def invite_ttl(max_age: int | None) -> str:
    if not max_age:
        return "бессрочно"
    return duration_label(max_age)


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def join_card(member: discord.Member, invite: UsedInvite | None) -> ui.LayoutView:
    fields: dict[str, object] = {
        "Кто": user_label(member),
        "Аккаунт": f"{stamp(member.created_at, 'D')} · {age_label(member.created_at)}",
        "Вход": stamp_pair(member.joined_at),
        "Сейчас на сервере": member.guild.member_count,
    }
    if member.pending:
        fields["Проверка правил"] = "ещё не прошёл"
    if invite:
        if invite.vanity:
            fields["Инвайт"] = f"короткая ссылка сервера · использований {invite.uses}"
        else:
            who = f" · от {invite.inviter_name}" if invite.inviter_name else ""
            fields["Инвайт"] = f"`{invite.code}`{who} · использований {invite.uses}"
    return event("Вход на сервер", tone=Tone.OK, fields=fields, source=FOOTER_USER)


def leave_card(member: discord.Member) -> ui.LayoutView:
    fields: dict[str, object] = {"Кто": user_label(member)}
    if member.joined_at:
        fields["Был на сервере"] = age_label(member.joined_at)
    fields["Осталось"] = member.guild.member_count
    roles = [role.mention for role in member.roles if not role.is_default()]
    if roles:
        fields["Роли"] = truncate(" ".join(roles), 800)
    if member.premium_since:
        fields["Буст с"] = stamp(member.premium_since, "D")
    return event("Выход с сервера", tone=Tone.DANGER, fields=fields, source=FOOTER_USER)


def delete_card(
    cached: CachedMessage | None,
    payload: discord.RawMessageDeleteEvent,
    *,
    channel: discord.abc.GuildChannel | discord.Thread | None = None,
) -> ui.LayoutView:
    fields: dict[str, object] = {"Канал": channel_label(channel, payload.channel_id)}
    if cached is None:
        fields["Сообщение"] = f"`{payload.message_id}` · текста нет в кэше бота"
        return event("Сообщение удалено", tone=Tone.DANGER, fields=fields, source=FOOTER_USER)
    fields["Автор"] = user_label(None, cached.author_id)
    fields["Написано"] = stamp_pair(cached.created_at)
    extras: list[str] = []
    if cached.stickers:
        extras.append("стикеры: " + ", ".join(cached.stickers))
    if cached.attachments:
        extras.append("файлы: " + ", ".join(cached.attachments))
    if cached.reply_id:
        extras.append(f"ответ на `{cached.reply_id}`")
    if extras:
        fields["Ещё"] = truncate(" · ".join(extras), 500)
    if cached.jump_url:
        fields["Где было"] = jump_link(cached.jump_url)
    return event(
        "Сообщение удалено",
        quote_block(cached.content),
        tone=Tone.DANGER,
        fields=fields,
        source=FOOTER_USER,
    )


def edit_card(
    *,
    channel: discord.abc.GuildChannel | discord.Thread | None,
    channel_id: int,
    author_id: int | None,
    author_name: str,
    before: str | None,
    after: str,
    jump: str,
) -> ui.LayoutView:
    fields: dict[str, object] = {
        "Канал": channel_label(channel, channel_id),
        "Автор": user_label(None, author_id) if author_id else author_name,
        "Было": quote_block(before) if before is not None else "*нет в кэше бота*",
        "Стало": quote_block(after),
        "Сообщение": jump_link(jump),
    }
    return event("Сообщение изменено", tone=Tone.INFO, fields=fields, source=FOOTER_USER)


def bulk_delete_card(
    *,
    channel: discord.abc.GuildChannel | discord.Thread | None,
    channel_id: int,
    deleted: int,
    recovered: list[str],
) -> ui.LayoutView:
    body = f"Удалено **{deleted}** сообщ. в {channel_label(channel, channel_id)}"
    fields = {"Что удалось восстановить": "\n".join(recovered[:12])} if recovered else None
    return event("Массовое удаление", body, tone=Tone.DANGER, fields=fields, source=FOOTER_USER)


def voice_card(
    member: discord.Member,
    *,
    title: str,
    lines: list[str],
    tone: Tone,
    stayed: str | None,
) -> ui.LayoutView:
    fields: dict[str, object] = {"Кто": user_label(member)}
    if stayed is not None:
        fields["Пробыл"] = stayed
    return event(
        title,
        _bullets(lines) if lines else "",
        tone=tone,
        fields=fields,
        source=FOOTER_USER,
    )


def profile_card(
    member: discord.Member,
    changes: list[tuple[str, str]],
    *,
    thumbnail_url: str | None,
) -> ui.LayoutView:
    fields: dict[str, object] = {"Кто": user_label(member)}
    for name, value in changes:
        fields[name] = value
    return event(
        "Профиль участника",
        tone=Tone.INFO,
        fields=fields,
        source=FOOTER_USER,
        thumbnail_url=thumbnail_url,
    )


def reaction_add_card(*, member: discord.Member, emoji: str, channel: str, jump: str) -> ui.LayoutView:
    return event(
        "Реакция поставлена",
        tone=Tone.OK,
        fields={"Кто": user_label(member), "Эмодзи": emoji, "Канал": channel, "Сообщение": jump},
        source=FOOTER_USER,
    )


def reaction_remove_card(*, user_id: int, emoji: str, channel: str, jump: str) -> ui.LayoutView:
    return event(
        "Реакция снята",
        tone=Tone.DANGER,
        fields={"Кто": user_label(None, user_id), "Эмодзи": emoji, "Канал": channel, "Сообщение": jump},
        source=FOOTER_USER,
    )


def reaction_clear_card(*, channel: str, jump: str) -> ui.LayoutView:
    return event(
        "Все реакции сняты",
        tone=Tone.MUTED,
        fields={"Канал": channel, "Сообщение": jump},
        source=FOOTER_USER,
    )


def reaction_clear_emoji_card(*, emoji: str, jump: str) -> ui.LayoutView:
    return event(
        "Реакции одного эмодзи сняты",
        tone=Tone.MUTED,
        fields={"Эмодзи": emoji, "Сообщение": jump},
        source=FOOTER_USER,
    )


def invite_create_card(invite: discord.Invite) -> ui.LayoutView:
    return event(
        "Создан инвайт",
        tone=Tone.OK,
        fields={
            "Код": f"`{invite.code}`",
            "Кто": user_label(invite.inviter),
            "Канал": channel_label(invite.channel),
            "Макс. входов": invite.max_uses or "без лимита",
            "Срок": invite_ttl(invite.max_age),
            "Временный": "да" if invite.temporary else "нет",
        },
        source=FOOTER_USER,
    )


def invite_delete_card(invite: discord.Invite) -> ui.LayoutView:
    return event(
        "Инвайт удалён",
        tone=Tone.DANGER,
        fields={"Код": f"`{invite.code}`"},
        source=FOOTER_USER,
    )


def thread_create_card(thread: discord.Thread) -> ui.LayoutView:
    return event(
        "Создан тред",
        tone=Tone.OK,
        fields={
            "Тред": channel_label(thread),
            "Родитель": channel_label(thread.parent, thread.parent_id),
            "Кто": user_label(thread.owner),
        },
        source=FOOTER_USER,
    )


def thread_delete_card(thread: discord.Thread) -> ui.LayoutView:
    return event(
        "Тред удалён",
        tone=Tone.DANGER,
        fields={"Тред": f"**{thread.name}** · `{thread.id}`"},
        source=FOOTER_USER,
    )


def thread_update_card(thread: discord.Thread, bits: list[str]) -> ui.LayoutView:
    return event(
        "Тред изменён",
        _bullets(bits),
        tone=Tone.INFO,
        fields={"Тред": channel_label(thread)},
        source=FOOTER_USER,
    )


def channel_create_card(channel: discord.abc.GuildChannel) -> ui.LayoutView:
    return event(
        "Канал создан",
        tone=Tone.OK,
        fields={"Канал": channel_label(channel), "Тип": channel_type_label(channel)},
        source=FOOTER_USER,
    )


def channel_delete_card(channel: discord.abc.GuildChannel) -> ui.LayoutView:
    return event(
        "Канал удалён",
        tone=Tone.DANGER,
        fields={"Канал": f"**{channel.name}** · `{channel.id}`", "Тип": channel_type_label(channel)},
        source=FOOTER_USER,
    )


def channel_update_card(channel: discord.abc.GuildChannel, bits: list[str]) -> ui.LayoutView:
    return event(
        "Канал изменён",
        _bullets(bits),
        tone=Tone.INFO,
        fields={"Канал": channel_label(channel)},
        source=FOOTER_USER,
    )


def scheduled_event_create_card(scheduled: discord.ScheduledEvent) -> ui.LayoutView:
    return event(
        "Ивент создан",
        tone=Tone.OK,
        fields={
            "Название": scheduled.name,
            "Старт": stamp_pair(scheduled.start_time),
            "Кто": user_label(scheduled.creator),
        },
        source=FOOTER_USER,
    )


def scheduled_event_delete_card(scheduled: discord.ScheduledEvent) -> ui.LayoutView:
    return event(
        "Ивент удалён",
        tone=Tone.DANGER,
        fields={"Название": scheduled.name},
        source=FOOTER_USER,
    )


def stage_create_card(stage: discord.StageInstance) -> ui.LayoutView:
    return event(
        "Трибуна начата",
        tone=Tone.OK,
        fields={"Тема": stage.topic, "Канал": channel_label(stage.channel)},
        source=FOOTER_USER,
    )


def stage_delete_card(stage: discord.StageInstance) -> ui.LayoutView:
    return event(
        "Трибуна завершена",
        tone=Tone.DANGER,
        fields={"Тема": stage.topic},
        source=FOOTER_USER,
    )


def emoji_update_card(*, added: list[str], removed: list[str]) -> ui.LayoutView:
    fields: dict[str, object] = {}
    if added:
        fields["Добавлены"] = truncate(", ".join(added))
    if removed:
        fields["Удалены"] = truncate(", ".join(removed))
    return event("Эмодзи сервера", tone=Tone.INFO, fields=fields, source=FOOTER_USER)


def sticker_update_card(*, added: list[str], removed: list[str]) -> ui.LayoutView:
    fields: dict[str, object] = {}
    if added:
        fields["Добавлены"] = truncate(", ".join(added))
    if removed:
        fields["Удалены"] = truncate(", ".join(removed))
    return event("Стикеры сервера", tone=Tone.INFO, fields=fields, source=FOOTER_USER)


def guild_update_card(bits: list[str]) -> ui.LayoutView:
    return event("Сервер изменён", _bullets(bits), tone=Tone.INFO, source=FOOTER_USER)


def webhooks_card(channel: discord.abc.GuildChannel) -> ui.LayoutView:
    return event(
        "Вебхуки изменены",
        tone=Tone.MUTED,
        fields={"Канал": channel_label(channel)},
        source=FOOTER_USER,
    )


def loyalty_revert_card(
    member: discord.Member,
    *,
    actor: discord.abc.User | None,
    actor_id: int | None,
    description: str,
    granted: tuple[int, ...],
    removed: tuple[int, ...],
    bot_removed: str | None,
    bot_restored: str | None,
) -> ui.LayoutView:
    fields: dict[str, object] = {
        "Участник": user_label(member),
        "Кто изменил": user_label(actor, actor_id),
    }
    if granted:
        fields["Выдали вручную"] = " ".join(f"`{role_id}`" for role_id in granted)
    if removed:
        fields["Сняли вручную"] = " ".join(f"`{role_id}`" for role_id in removed)
    if bot_removed:
        fields["Снял бот"] = bot_removed
    if bot_restored:
        fields["Вернул бот"] = bot_restored
    return event(
        "Роль Old: запрещённое изменение",
        description,
        tone=Tone.DANGER,
        fields=fields,
        source=FOOTER_LOYALTY,
    )
