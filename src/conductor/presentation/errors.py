"""Карточка критического сбоя для лога администрации. Без sqlalchemy и services."""

from __future__ import annotations

from discord import ui

from conductor.domain.errors import CriticalError
from conductor.presentation import copy
from conductor.presentation.layout import event
from conductor.presentation.tokens import Tone

__all__ = ["critical_error_card"]


def critical_error_card(report: CriticalError) -> ui.LayoutView:
    who = _who(report)
    where = _where(report)
    action = report.action or "—"
    if report.extras:
        action = f"{action} · {report.extras}"
    fields: dict[str, object] = {
        copy.ERROR_FIELD_CODE: report.code,
        copy.ERROR_FIELD_ACTION: f"{action} · {copy.error_kind_label(report.kind)}",
        copy.ERROR_FIELD_WHERE: where,
        copy.ERROR_FIELD_WHO: who,
        copy.ERROR_FIELD_PERMS: copy.error_perm_list(report.discord_perms),
    }
    if report.missing_bot_perms:
        fields[copy.ERROR_FIELD_BOT] = copy.error_perm_list(report.missing_bot_perms)
    if report.origin:
        fields[copy.ERROR_FIELD_ORIGIN] = report.origin
    detail = report.error_type
    if report.error_text:
        detail = f"{detail}: {report.error_text}"
    fields[copy.ERROR_FIELD_DETAIL] = detail
    return event(
        copy.error_title(report.kind),
        tone=Tone.DANGER,
        fields=fields,
        source=copy.ERROR_LOG_SOURCE,
    )


def _who(report: CriticalError) -> str:
    name = report.user_name or (f"`{report.user_id}`" if report.user_id else "—")
    if report.user_id is not None:
        name = f"<@{report.user_id}> · {name}"
    marks: list[str] = []
    if report.is_developer:
        marks.append(copy.ERROR_DEVELOPER)
    if report.is_guild_owner:
        marks.append(copy.ERROR_GUILD_OWNER)
    if report.is_administrator:
        marks.append(copy.ERROR_DISCORD_ADMIN)
    marks.append(copy.rank_label(report.rank))
    return f"{name} · {' · '.join(marks)}"


def _where(report: CriticalError) -> str:
    if report.guild_id is None and not report.channel_name:
        return copy.ERROR_DM
    bits: list[str] = []
    if report.channel_name:
        channel = f"#{report.channel_name}" if not report.channel_name.startswith("#") else report.channel_name
        if report.channel_kind:
            channel = f"{channel} · {report.channel_kind}"
        bits.append(channel)
    elif report.channel_id is not None:
        bits.append(f"<#{report.channel_id}>")
    if report.guild_name:
        guild = report.guild_name
        if report.guild_id is not None:
            guild = f"{guild} (`{report.guild_id}`)"
        bits.append(guild)
    elif report.guild_id is not None:
        bits.append(f"`{report.guild_id}`")
    return " · ".join(bits) if bits else copy.ERROR_DM
