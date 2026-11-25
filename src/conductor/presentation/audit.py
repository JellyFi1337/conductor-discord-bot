from __future__ import annotations

from datetime import datetime
from typing import Any

import discord
from discord import ui

from conductor.presentation.layout import event
from conductor.presentation.text import FOOTER_ADMIN, channel_label, stamp, truncate, user_label
from conductor.presentation.tokens import Tone

CHANGE_LABELS = {
    "nick": "ник",
    "name": "имя",
    "colour": "цвет",
    "color": "цвет",
    "permissions": "права",
    "mentionable": "упоминаемая",
    "hoist": "отдельно в списке",
    "mute": "мут",
    "deaf": "глухота",
    "topic": "описание",
    "nsfw": "возрастная метка",
    "bitrate": "битрейт",
    "user_limit": "лимит участников",
    "slowmode_delay": "медленный режим",
    "timed_out_until": "тайм-аут",
    "communication_disabled_until": "тайм-аут",
    "roles": "роли",
}

EXTRA_FIELDS = (
    ("channel", "Канал"),
    ("count", "Сколько"),
    ("delete_member_days", "Дней бездействия"),
    ("members_removed", "Исключено"),
    ("message_id", "Сообщение"),
    ("automod_rule_name", "Правило"),
    ("automod_rule_trigger_type", "Триггер"),
    ("integration_type", "Интеграция"),
)

SKIP_IF_USER_LOGS_SAME_CHANNEL = frozenset(
    {
        "member_update",
        "message_delete",
        "message_bulk_delete",
        "member_move",
        "member_disconnect",
        "invite_create",
        "invite_update",
        "invite_delete",
        "channel_create",
        "channel_update",
        "channel_delete",
        "thread_create",
        "thread_update",
        "thread_delete",
        "emoji_create",
        "emoji_update",
        "emoji_delete",
        "sticker_create",
        "sticker_update",
        "sticker_delete",
        "guild_update",
        "scheduled_event_create",
        "scheduled_event_update",
        "scheduled_event_delete",
        "stage_instance_create",
        "stage_instance_update",
        "stage_instance_delete",
        "webhook_create",
        "webhook_update",
        "webhook_delete",
    }
)

SKIP_AUDIT_ALWAYS = frozenset(
    {
        "automod_block_message",
        "automod_flag_message",
        "automod_timeout_member",
        "automod_quarantine_user",
    }
)


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == ()


def format_audit_value(value: Any) -> str:
    if _is_empty(value):
        return "—"
    if isinstance(value, datetime):
        return stamp(value)
    if isinstance(value, discord.Permissions):
        names = [name for name, allowed in value if allowed]
        return truncate(", ".join(names) or "нет", 400)
    if isinstance(value, discord.Role):
        return f"{value.mention} · `{value.id}`"
    if isinstance(value, discord.abc.User):
        return user_label(value)
    if isinstance(value, discord.abc.GuildChannel | discord.Thread):
        return channel_label(value)
    if isinstance(value, discord.Object):
        ident = value.id
        kind = getattr(value, "type", None)
        if kind is discord.Role:
            return f"<@&{ident}> · `{ident}`"
        if kind in {discord.Member, discord.User, discord.abc.User}:
            return f"<@{ident}> · `{ident}`"
        return f"`{ident}`"
    if isinstance(value, list | tuple | set):
        parts = [format_audit_value(item) for item in value if not _is_empty(item)]
        return truncate(", ".join(parts) or "—", 400)
    mention = getattr(value, "mention", None)
    ident = getattr(value, "id", None)
    if isinstance(mention, str) and ident is not None:
        return f"{mention} · `{ident}`"
    text = str(value)
    if "audit_logs._AuditLog" in text or text.startswith("<discord."):
        return "—"
    return truncate(text, 400)


def format_audit_extra(extra: object) -> list[str]:
    if extra is None:
        return []
    if isinstance(extra, discord.Role | discord.abc.User | discord.Object):
        return [f"**цель:** {format_audit_value(extra)}"]
    lines: list[str] = []
    for attr, label in EXTRA_FIELDS:
        if not hasattr(extra, attr):
            continue
        value = getattr(extra, attr)
        if _is_empty(value):
            continue
        if attr == "channel":
            channel_id = getattr(value, "id", None)
            resolved = value if isinstance(value, discord.abc.GuildChannel | discord.Thread) else None
            lines.append(f"**{label}:** {channel_label(resolved, channel_id)}")
            continue
        if attr == "count":
            lines.append(f"**{label}:** {value}")
            continue
        if attr == "message_id":
            lines.append(f"**{label}:** `{value}`")
            continue
        lines.append(f"**{label}:** {format_audit_value(value)}")
    return lines


def _as_items(value: object) -> list[object]:
    if _is_empty(value):
        return []
    if isinstance(value, list | tuple | set):
        return [item for item in value if not _is_empty(item)]
    return [value]


def _item_key(item: object) -> object:
    return getattr(item, "id", item)


def _role_delta_lines(before: object, after: object) -> list[str]:
    old_items = _as_items(before)
    new_items = _as_items(after)
    old_keys = {_item_key(item) for item in old_items}
    new_keys = {_item_key(item) for item in new_items}
    added = [item for item in new_items if _item_key(item) not in old_keys]
    removed = [item for item in old_items if _item_key(item) not in new_keys]
    lines: list[str] = []
    if added:
        lines.append(f"**Выданы:** {format_audit_value(added)}")
    if removed:
        lines.append(f"**Сняты:** {format_audit_value(removed)}")
    return lines


def _diff_dict(diff: object) -> dict[str, object]:
    if not diff:
        return {}
    try:
        return dict(diff)
    except (TypeError, ValueError):
        return {}


def format_audit_changes(entry: object) -> str:
    before = _diff_dict(getattr(entry, "before", None))
    after = _diff_dict(getattr(entry, "after", None))
    keys = set(before) | set(after)
    lines: list[str] = []
    if "roles" in keys:
        lines.extend(_role_delta_lines(before.get("roles"), after.get("roles")))
        keys.discard("roles")
    for key in sorted(keys, key=str):
        label = CHANGE_LABELS.get(str(key), str(key).replace("_", " "))
        old = before.get(key)
        new = after.get(key)
        if _is_empty(old) and _is_empty(new):
            continue
        lines.append(f"**{label}:** {format_audit_value(old)} → {format_audit_value(new)}")
    lines.extend(format_audit_extra(getattr(entry, "extra", None)))
    reason = getattr(entry, "reason", None)
    if reason:
        lines.append(f"**Причина:** {truncate(str(reason), 300)}")
    if not lines:
        return ""
    return "\n".join(lines)[:3900]


def audit_card(entry: discord.AuditLogEntry, title: str) -> ui.LayoutView:
    fields: dict[str, object] = {"Кто сделал": user_label(entry.user, entry.user_id)}
    target = entry.target
    target_id = getattr(target, "id", None) or getattr(entry, "target_id", None)
    if isinstance(target, discord.abc.GuildChannel):
        fields["Цель"] = channel_label(target)
    elif isinstance(target, discord.Role):
        fields["Цель"] = f"{target.mention} · `{target.id}`"
    elif target is not None:
        label = target if isinstance(target, discord.abc.User) else None
        fields["Цель"] = user_label(label, target_id)
    return event(
        title,
        format_audit_changes(entry),
        tone=Tone.DANGER,
        fields=fields,
        source=FOOTER_ADMIN,
        when=entry.created_at,
    )


def automod_card(execution: discord.AutoModAction) -> ui.LayoutView:
    fields: dict[str, object] = {
        "Правило": f"`{execution.rule_id}`",
        "Кто": user_label(None, execution.user_id),
        "Канал": channel_label(execution.channel, execution.channel_id),
    }
    if execution.content:
        fields["Текст"] = truncate(execution.content, 500)
    return event("Сработал автомод", tone=Tone.DANGER, fields=fields, source=FOOTER_ADMIN)
