"""Снапшоты карточек логов, перечень событий и запрет discord.Embed."""

from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from discord import ui

from conductor.cogs.admin_logs import ACTION_TITLES, AdminLogs
from conductor.cogs.user_logs import UserLogs
from conductor.domain.records import CachedMessage, UsedInvite
from conductor.presentation.audit import audit_card, automod_card
from conductor.presentation.events import (
    bulk_delete_card,
    channel_create_card,
    channel_delete_card,
    channel_update_card,
    delete_card,
    edit_card,
    emoji_update_card,
    guild_update_card,
    invite_create_card,
    invite_delete_card,
    is_bot_message_edit,
    join_card,
    leave_card,
    loyalty_revert_card,
    profile_card,
    reaction_add_card,
    reaction_clear_card,
    reaction_clear_emoji_card,
    reaction_remove_card,
    scheduled_event_create_card,
    scheduled_event_delete_card,
    stage_create_card,
    stage_delete_card,
    sticker_update_card,
    thread_create_card,
    thread_delete_card,
    thread_update_card,
    voice_card,
    webhooks_card,
)
from conductor.presentation.layout import collected_text
from conductor.presentation.text import FOOTER_ADMIN, FOOTER_LOYALTY, FOOTER_USER
from conductor.presentation.tokens import Icon, Tone
from conductor.services.message_cache import MessageCache

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
STAMP = datetime(2024, 6, 1, 15, 30, tzinfo=UTC)
_HEX = re.compile(r"0x[0-9A-Fa-f]+")

USER_LOG_LISTENERS = (
    "on_guild_channel_create",
    "on_guild_channel_delete",
    "on_guild_channel_update",
    "on_guild_emojis_update",
    "on_guild_stickers_update",
    "on_guild_update",
    "on_invite_create",
    "on_invite_delete",
    "on_member_join",
    "on_member_remove",
    "on_member_update",
    "on_message",
    "on_raw_bulk_message_delete",
    "on_raw_message_delete",
    "on_raw_message_edit",
    "on_raw_reaction_add",
    "on_raw_reaction_clear",
    "on_raw_reaction_clear_emoji",
    "on_raw_reaction_remove",
    "on_scheduled_event_create",
    "on_scheduled_event_delete",
    "on_stage_instance_create",
    "on_stage_instance_delete",
    "on_thread_create",
    "on_thread_delete",
    "on_thread_update",
    "on_voice_state_update",
    "on_webhooks_update",
)

ADMIN_LOG_LISTENERS = (
    "on_audit_log_entry_create",
    "on_automod_action",
)

ADMIN_ACTION_KEYS = frozenset(
    {
        "guild_update",
        "channel_create",
        "channel_update",
        "channel_delete",
        "overwrite_create",
        "overwrite_update",
        "overwrite_delete",
        "kick",
        "member_prune",
        "ban",
        "unban",
        "member_update",
        "member_role_update",
        "member_move",
        "member_disconnect",
        "bot_add",
        "role_create",
        "role_update",
        "role_delete",
        "invite_create",
        "invite_update",
        "invite_delete",
        "webhook_create",
        "webhook_update",
        "webhook_delete",
        "emoji_create",
        "emoji_update",
        "emoji_delete",
        "message_delete",
        "message_bulk_delete",
        "message_pin",
        "message_unpin",
        "integration_create",
        "integration_update",
        "integration_delete",
        "stage_instance_create",
        "stage_instance_update",
        "stage_instance_delete",
        "sticker_create",
        "sticker_update",
        "sticker_delete",
        "scheduled_event_create",
        "scheduled_event_update",
        "scheduled_event_delete",
        "thread_create",
        "thread_update",
        "thread_delete",
        "app_command_permission_update",
        "soundboard_sound_create",
        "soundboard_sound_update",
        "soundboard_sound_delete",
        "automod_rule_create",
        "automod_rule_update",
        "automod_rule_delete",
        "onboarding_prompt_create",
        "onboarding_prompt_update",
        "onboarding_prompt_delete",
        "onboarding_create",
        "onboarding_update",
        "home_settings_create",
        "home_settings_update",
    }
)

EVENT_CARD_KINDS = frozenset(
    {
        "audit",
        "automod",
        "bulk_delete",
        "channel_create",
        "channel_delete",
        "channel_update",
        "delete",
        "delete_uncached",
        "edit",
        "emoji",
        "guild_update",
        "invite_create",
        "invite_delete",
        "join",
        "leave",
        "loyalty_revert",
        "profile",
        "reaction_add",
        "reaction_clear",
        "reaction_clear_emoji",
        "reaction_remove",
        "scheduled_create",
        "scheduled_delete",
        "stage_create",
        "stage_delete",
        "sticker",
        "thread_create",
        "thread_delete",
        "thread_update",
        "voice_flags",
        "voice_join",
        "voice_leave",
        "voice_move",
        "webhooks",
    }
)


def _listeners(cls: type) -> tuple[str, ...]:
    return tuple(sorted(name for name, _ in inspect.getmembers(cls, inspect.isfunction) if name.startswith("on_")))


def _member(**kwargs: object) -> SimpleNamespace:
    data: dict[str, object] = {
        "id": 7,
        "mention": "<@7>",
        "display_name": "Ann",
        "name": "Ann",
        "created_at": STAMP,
        "joined_at": STAMP,
        "pending": False,
        "premium_since": None,
        "roles": [],
        "guild": SimpleNamespace(member_count=12, id=1),
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def _channel() -> SimpleNamespace:
    return SimpleNamespace(id=3, mention="<#3>", name="общий", type="text", guild=SimpleNamespace(id=1))


def _cached(*, is_bot: bool = False, content: str = "привет") -> CachedMessage:
    return CachedMessage(
        id=99,
        guild_id=1,
        channel_id=3,
        author_id=7,
        author_name="Ann",
        content=content,
        created_at=STAMP,
        attachments=("a.png",),
        jump_url="https://discord.com/channels/1/3/99",
        stickers=("hi",),
        reply_id=8,
        is_bot=is_bot,
    )


def _assert_static(view: ui.LayoutView, *, source: str) -> str:
    assert view.timeout is None
    assert not any(isinstance(item, ui.Button) for item in view.walk_children())
    text = collected_text(view)
    assert text.startswith("## ")
    assert f"-# {source} ·" in text
    assert "<t:" in text
    assert "None" not in text
    assert _HEX.search(text) is None
    assert "LayoutView" not in text
    assert Icon.WAIT not in text
    return text


def _all_cards() -> dict[str, ui.LayoutView]:
    member = _member()
    channel = _channel()
    invite = SimpleNamespace(
        code="abc",
        inviter=SimpleNamespace(id=7),
        channel=channel,
        max_uses=0,
        max_age=0,
        temporary=False,
    )
    thread = SimpleNamespace(
        id=8,
        name="тред",
        mention="<#8>",
        parent=channel,
        parent_id=3,
        owner=SimpleNamespace(id=7),
        guild=SimpleNamespace(id=1),
    )
    scheduled = SimpleNamespace(name="сбор", start_time=STAMP, creator=SimpleNamespace(id=7))
    stage = SimpleNamespace(topic="тема", channel=channel, guild=SimpleNamespace(id=1))
    jump = "[перейти](https://discord.com/channels/1/3/99)"
    used = UsedInvite(code="abc", inviter_id=7, inviter_name="Ann", uses=4, max_uses=None, channel_id=3)
    entry = SimpleNamespace(
        user=SimpleNamespace(id=2),
        user_id=2,
        target=SimpleNamespace(id=7),
        target_id=7,
        created_at=STAMP,
        before={"nick": "старый"},
        after={"nick": "новый"},
        extra=None,
        reason="спам",
    )
    execution = SimpleNamespace(
        rule_id=55,
        user_id=7,
        channel=channel,
        channel_id=3,
        content="плохое слово",
        guild_id=1,
    )
    return {
        "join": join_card(member, used),  # type: ignore[arg-type]
        "leave": leave_card(member),  # type: ignore[arg-type]
        "delete": delete_card(_cached(), SimpleNamespace(channel_id=3, message_id=99), channel=channel),  # type: ignore[arg-type]
        "delete_uncached": delete_card(None, SimpleNamespace(channel_id=3, message_id=99), channel=channel),  # type: ignore[arg-type]
        "edit": edit_card(
            channel=channel,  # type: ignore[arg-type]
            channel_id=3,
            author_id=7,
            author_name="Ann",
            before="было",
            after="стало",
            jump="https://discord.com/channels/1/3/99",
        ),
        "bulk_delete": bulk_delete_card(
            channel=channel,  # type: ignore[arg-type]
            channel_id=3,
            deleted=4,
            recovered=["**Ann:** привет"],
        ),
        "profile": profile_card(member, [("Ник", "Ann → Ann2")], thumbnail_url=None),  # type: ignore[arg-type]
        "voice_join": voice_card(member, title="Вошёл в голосовой", lines=["<#3>"], tone=Tone.OK, stayed=None),  # type: ignore[arg-type]
        "voice_leave": voice_card(
            member,  # type: ignore[arg-type]
            title="Вышел из голосового",
            lines=["<#3>"],
            tone=Tone.DANGER,
            stayed="2 мин",
        ),
        "voice_move": voice_card(
            member,  # type: ignore[arg-type]
            title="Сменил голосовой",
            lines=["<#3> → <#4>"],
            tone=Tone.WARN,
            stayed="1 мин",
        ),
        "voice_flags": voice_card(
            member,  # type: ignore[arg-type]
            title="Голосовой канал",
            lines=["микрофон: выкл"],
            tone=Tone.MUTED,
            stayed=None,
        ),
        "reaction_add": reaction_add_card(member=member, emoji="👍", channel="<#3>", jump=jump),  # type: ignore[arg-type]
        "reaction_remove": reaction_remove_card(user_id=7, emoji="👍", channel="<#3>", jump=jump),
        "reaction_clear": reaction_clear_card(channel="<#3>", jump=jump),
        "reaction_clear_emoji": reaction_clear_emoji_card(emoji="👍", jump=jump),
        "invite_create": invite_create_card(invite),  # type: ignore[arg-type]
        "invite_delete": invite_delete_card(invite),  # type: ignore[arg-type]
        "thread_create": thread_create_card(thread),  # type: ignore[arg-type]
        "thread_delete": thread_delete_card(thread),  # type: ignore[arg-type]
        "thread_update": thread_update_card(thread, ["архивирован"]),  # type: ignore[arg-type]
        "channel_create": channel_create_card(channel),  # type: ignore[arg-type]
        "channel_delete": channel_delete_card(channel),  # type: ignore[arg-type]
        "channel_update": channel_update_card(channel, ["имя: a → b"]),  # type: ignore[arg-type]
        "scheduled_create": scheduled_event_create_card(scheduled),  # type: ignore[arg-type]
        "scheduled_delete": scheduled_event_delete_card(scheduled),  # type: ignore[arg-type]
        "stage_create": stage_create_card(stage),  # type: ignore[arg-type]
        "stage_delete": stage_delete_card(stage),  # type: ignore[arg-type]
        "emoji": emoji_update_card(added=["grin"], removed=["sad"]),
        "sticker": sticker_update_card(added=["wave"], removed=[]),
        "guild_update": guild_update_card(["имя: Old → New"]),
        "webhooks": webhooks_card(channel),  # type: ignore[arg-type]
        "audit": audit_card(entry, "Изменение участника"),  # type: ignore[arg-type]
        "automod": automod_card(execution),  # type: ignore[arg-type]
        "loyalty_revert": loyalty_revert_card(
            member,  # type: ignore[arg-type]
            actor=None,
            actor_id=2,
            description=(
                "Роли Old нельзя выдавать и снимать вручную. Действие записано, состояние возвращено по реестру."
            ),
            granted=(10,),
            removed=(),
            bot_removed=" <@&10>",
            bot_restored=None,
        ),
    }


def test_user_and_admin_listeners_match_pre_migration_inventory() -> None:
    assert _listeners(UserLogs) == USER_LOG_LISTENERS
    assert _listeners(AdminLogs) == ADMIN_LOG_LISTENERS
    assert frozenset(ACTION_TITLES) == ADMIN_ACTION_KEYS
    assert ACTION_TITLES["ban"] == "Бан"
    assert ACTION_TITLES["member_role_update"] == "Выдача / снятие ролей"


def test_every_event_type_has_a_static_snapshot() -> None:
    cards = _all_cards()
    assert set(cards) == EVENT_CARD_KINDS
    sources = {
        "audit": FOOTER_ADMIN,
        "automod": FOOTER_ADMIN,
        "loyalty_revert": FOOTER_LOYALTY,
    }
    snapshots: dict[str, str] = {}
    for kind, view in cards.items():
        snapshots[kind] = _assert_static(view, source=sources.get(kind, FOOTER_USER))
    assert snapshots["join"].splitlines()[0] == f"## {Icon.DONE} Вход на сервер"
    assert "**Кто:**" in snapshots["join"]
    assert snapshots["leave"].splitlines()[0] == f"## {Icon.DENY} Выход с сервера"
    assert snapshots["delete"].splitlines()[0] == f"## {Icon.DENY} Сообщение удалено"
    assert "> привет" in snapshots["delete"]
    assert "**Автор:**" in snapshots["delete"]
    assert snapshots["edit"].splitlines()[0] == f"## {Icon.PANEL} Сообщение изменено"
    assert "> было" in snapshots["edit"]
    assert snapshots["bulk_delete"].splitlines()[0] == f"## {Icon.DENY} Массовое удаление"
    assert snapshots["voice_join"].splitlines()[0] == f"## {Icon.DONE} Вошёл в голосовой"
    assert snapshots["voice_leave"].splitlines()[0] == f"## {Icon.DENY} Вышел из голосового"
    assert snapshots["voice_move"].splitlines()[0] == f"## {Icon.WARN} Сменил голосовой"
    assert snapshots["reaction_add"].splitlines()[0] == f"## {Icon.DONE} Реакция поставлена"
    assert snapshots["invite_create"].splitlines()[0] == f"## {Icon.DONE} Создан инвайт"
    assert snapshots["audit"].splitlines()[0] == f"## {Icon.DENY} Изменение участника"
    assert "**Причина:** спам" in snapshots["audit"]
    assert snapshots["automod"].splitlines()[0] == f"## {Icon.DENY} Сработал автомод"
    assert snapshots["loyalty_revert"].splitlines()[0] == f"## {Icon.DENY} Роль Old: запрещённое изменение"


@pytest.mark.parametrize(
    ("data", "cached", "skip"),
    [
        ({"author": {"bot": True, "id": "1"}, "content": "x"}, None, True),
        ({"author": {"bot": False, "id": "7"}, "content": "x"}, None, False),
        ({"author": {"id": "7"}, "content": "x"}, None, False),
        ({"content": "x"}, None, True),
        ({}, None, True),
        ({"content": "x"}, _cached(is_bot=True), True),
        ({"content": "x"}, _cached(is_bot=False), False),
        ({"webhook_id": "9", "content": "x"}, _cached(is_bot=False), True),
    ],
)
def test_bot_edits_are_skipped_without_author(data: dict, cached: CachedMessage | None, skip: bool) -> None:
    assert is_bot_message_edit(data, cached) is skip


@pytest.mark.asyncio
async def test_raw_edit_without_author_does_not_log_bot_cache() -> None:
    pump = MagicMock()
    cached = _cached(is_bot=True, content="старое")
    bot = SimpleNamespace(
        config_by_guild={},
        messages=SimpleNamespace(get=lambda _mid: cached, store=lambda _msg: None),
        log_pump=pump,
        get_guild=lambda _gid: None,
    )
    cog = UserLogs(bot)  # type: ignore[arg-type]
    payload = SimpleNamespace(guild_id=1, channel_id=3, message_id=99, data={"content": "новое"})
    await cog.on_raw_message_edit(payload)
    pump.submit.assert_not_called()


@pytest.mark.asyncio
async def test_raw_edit_without_author_logs_human_cache() -> None:
    pump = MagicMock()
    cached = _cached(is_bot=False, content="старое")
    bot = SimpleNamespace(
        config_by_guild={},
        messages=SimpleNamespace(get=lambda _mid: cached, store=lambda _msg: None),
        log_pump=pump,
        get_guild=lambda _gid: None,
    )
    cog = UserLogs(bot)  # type: ignore[arg-type]
    payload = SimpleNamespace(guild_id=1, channel_id=3, message_id=99, data={"content": "новое"})
    await cog.on_raw_message_edit(payload)
    pump.submit.assert_called_once()
    assert pump.submit.call_args.kwargs["view"] is not None
    assert "embed" not in pump.submit.call_args.kwargs


def _user_logs_bot(*, messages: object, pump: MagicMock, user_id: int = 99) -> SimpleNamespace:
    return SimpleNamespace(
        config_by_guild={},
        messages=messages,
        log_pump=pump,
        get_guild=lambda _gid: None,
        user=SimpleNamespace(id=user_id),
    )


@pytest.mark.asyncio
async def test_scheduled_event_without_guild_cache_uses_guild_id() -> None:
    pump = MagicMock()
    cog = UserLogs(_user_logs_bot(messages=SimpleNamespace(), pump=pump))  # type: ignore[arg-type]
    scheduled = SimpleNamespace(
        guild=None,
        guild_id=1,
        name="рейд",
        start_time=STAMP,
        creator=SimpleNamespace(id=7),
    )
    await cog.on_scheduled_event_create(scheduled)
    pump.submit.assert_called_once()


@pytest.mark.asyncio
async def test_scheduled_event_without_guild_id_is_ignored() -> None:
    pump = MagicMock()
    cog = UserLogs(_user_logs_bot(messages=SimpleNamespace(), pump=pump))  # type: ignore[arg-type]
    scheduled = SimpleNamespace(guild=None, guild_id=None, name="рейд", start_time=STAMP, creator=None)
    await cog.on_scheduled_event_create(scheduled)
    pump.submit.assert_not_called()


@pytest.mark.asyncio
async def test_stage_without_guild_id_is_ignored() -> None:
    pump = MagicMock()
    cog = UserLogs(_user_logs_bot(messages=SimpleNamespace(), pump=pump))  # type: ignore[arg-type]
    await cog.on_stage_instance_create(SimpleNamespace(guild=None, guild_id=None, topic="тема", channel=None))
    pump.submit.assert_not_called()


@pytest.mark.asyncio
async def test_reaction_remove_skips_bot() -> None:
    pump = MagicMock()
    cog = UserLogs(_user_logs_bot(messages=SimpleNamespace(), pump=pump, user_id=99))  # type: ignore[arg-type]
    payload = SimpleNamespace(
        guild_id=1,
        channel_id=3,
        message_id=99,
        user_id=99,
        member=SimpleNamespace(bot=True),
        emoji="👍",
    )
    await cog.on_raw_reaction_remove(payload)
    pump.submit.assert_not_called()
    payload.member = None
    await cog.on_raw_reaction_remove(payload)
    pump.submit.assert_not_called()


@pytest.mark.asyncio
async def test_delete_skips_bot_cache() -> None:
    pump = MagicMock()
    cached = _cached(is_bot=True)
    bot = _user_logs_bot(messages=SimpleNamespace(pop=lambda _mid: cached), pump=pump)
    cog = UserLogs(bot)  # type: ignore[arg-type]
    payload = SimpleNamespace(guild_id=1, channel_id=3, message_id=99, cached_message=None)
    await cog.on_raw_message_delete(payload)
    pump.submit.assert_not_called()


@pytest.mark.asyncio
async def test_edit_without_cache_stores_snapshot_for_later_delete() -> None:
    pump = MagicMock()
    cache = MessageCache(maxlen=20, ttl=60)
    cog = UserLogs(_user_logs_bot(messages=cache, pump=pump))  # type: ignore[arg-type]
    await cog.on_raw_message_edit(
        SimpleNamespace(
            guild_id=1,
            channel_id=3,
            message_id=99,
            data={"content": "новое", "author": {"id": "7", "username": "Ann", "bot": False}},
        )
    )
    pump.submit.assert_called_once()
    stored = cache.get(99)
    assert stored is not None
    assert stored.content == "новое"
    assert stored.is_bot is False
    pump.reset_mock()
    await cog.on_raw_message_delete(SimpleNamespace(guild_id=1, channel_id=3, message_id=99, cached_message=None))
    pump.submit.assert_called_once()
    text = collected_text(pump.submit.call_args.kwargs["view"])
    assert "нет в кэше" not in text
    assert "> новое" in text


def test_admin_skips_member_update_when_logs_share_a_channel() -> None:
    shared = SimpleNamespace(user_log_channel_id=5, admin_log_channel_id=5)
    split = SimpleNamespace(user_log_channel_id=5, admin_log_channel_id=6)
    entry = SimpleNamespace(
        action=SimpleNamespace(name="member_update"),
        guild=SimpleNamespace(id=1, me=SimpleNamespace(id=99)),
        user=SimpleNamespace(id=2),
        before={"nick": "a"},
        after={"nick": "b"},
        extra=None,
        reason=None,
    )
    assert AdminLogs(SimpleNamespace(config_by_guild={1: shared}))._skip_audit(entry) is True  # type: ignore[arg-type]
    assert AdminLogs(SimpleNamespace(config_by_guild={1: split}))._skip_audit(entry) is False  # type: ignore[arg-type]


def test_no_discord_embed_in_source() -> None:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            attr_embed = isinstance(node, ast.Attribute) and node.attr == "Embed"
            name_embed = isinstance(node, ast.Name) and node.id == "Embed"
            if attr_embed or name_embed:
                hits.append(f"{rel}:{node.lineno}")
    assert not hits, "discord.Embed запрещён после секции 9c:\n  " + "\n  ".join(hits)
