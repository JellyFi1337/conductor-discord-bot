"""Карточки /owner export и /owner apply. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import discord
from discord import app_commands, ui

from conductor.domain.blueprint import (
    BLUEPRINT_MAX_BYTES,
    EXPORT_ROOTS,
    BlueprintPlan,
    BlueprintReport,
    IssueLevel,
    attachment_error,
    wanted_from_bytes,
)
from conductor.presentation import copy
from conductor.presentation.admin import send_export
from conductor.presentation.interact import ack, instant
from conductor.presentation.layout import denied, panel, respond, update_message
from conductor.presentation.motion import InteractionTarget
from conductor.presentation.ops import ops_cancelled
from conductor.presentation.tokens import Tone

OwnerClick = Callable[[discord.Interaction], Awaitable[bool | None]]
SameOwner = Callable[[discord.Interaction, int], bool]


class BlueprintCommands(Protocol):
    async def export(self, guild: discord.Guild, *, with_docs: bool = True, only: tuple[str, ...] = ()) -> bytes: ...

    async def preview(self, guild: discord.Guild, raw: bytes) -> BlueprintPlan: ...

    async def apply(
        self,
        guild: discord.Guild,
        plan: BlueprintPlan,
        *,
        actor_id: int,
        build: bool = True,
        wanted: Mapping[str, Any] | None = None,
    ) -> BlueprintReport: ...


__all__ = [
    "APPLY_MODE_CHOICES",
    "EXPORT_SECTION_CHOICES",
    "blueprint_done",
    "blueprint_plan_card",
    "blueprint_rejected",
    "run_blueprint_apply",
    "run_blueprint_export",
]

EXPORT_SECTION_CHOICES = [app_commands.Choice(name=root, value=root) for root in EXPORT_ROOTS]
APPLY_MODE_CHOICES = [
    app_commands.Choice(name=copy.BLUEPRINT_CHECK, value="check"),
    app_commands.Choice(name=copy.BLUEPRINT_APPLY, value="apply"),
]
_PENDING: dict[tuple[int, int], bytes] = {}


def blueprint_rejected(code: str) -> ui.LayoutView:
    return panel(copy.BLUEPRINT_TITLE, copy.blueprint_issue_text(code), tone=Tone.DANGER)


def blueprint_plan_card(
    plan: BlueprintPlan,
    *,
    on_apply: OwnerClick | None,
    on_cancel: OwnerClick,
) -> ui.LayoutView:
    errors = tuple(item for item in plan.issues if item.level is IssueLevel.ERROR)
    warns = tuple(item for item in plan.issues if item.level is IssueLevel.WARN)
    body = copy.blueprint_plan_body(
        changes=len(plan.changes),
        creates=len(plan.creates),
        errors=len(errors),
        warns=len(warns),
        unknown=len(plan.unknown),
    )
    lines = [body]
    for item in (*errors, *warns)[:8]:
        lines.append(f"{item.path}: {copy.blueprint_issue_text(item.message)}")
    for path in plan.unknown[:4]:
        lines.append(path)
    tone = Tone.DANGER if plan.blocked else Tone.WARN if warns or plan.unknown else Tone.INFO
    rows: tuple[ui.ActionRow, ...] = ()
    if on_apply is not None and not plan.blocked:
        yes = ui.Button(label=copy.BLUEPRINT_APPLY[:80], style=discord.ButtonStyle.danger)
        no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
        yes.callback = instant(on_apply)
        no.callback = instant(on_cancel)
        rows = (ui.ActionRow(yes, no),)
    elif plan.blocked:
        no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
        no.callback = instant(on_cancel)
        rows = (ui.ActionRow(no),)
    return panel(copy.BLUEPRINT_TITLE, "\n".join(lines), tone=tone, rows=rows, timeout=60.0)


def blueprint_done(report: BlueprintReport) -> ui.LayoutView:
    if not report.applied and not report.failed:
        return panel(copy.BLUEPRINT_DONE, copy.BLUEPRINT_EMPTY, tone=Tone.OK)
    tone = Tone.DANGER if report.failed else Tone.OK
    body = copy.blueprint_report_body(
        applied=len(report.applied),
        skipped=len(report.skipped),
        failed=len(report.failed),
    )
    if report.notes:
        body = f"{body}\n" + "\n".join(report.notes[:5])
    return panel(copy.BLUEPRINT_DONE, body, tone=tone)


async def run_blueprint_export(
    interaction: discord.Interaction,
    *,
    service: BlueprintCommands,
    section: str | None,
    with_docs: bool,
) -> None:
    guild = interaction.guild
    assert guild is not None
    only = () if section is None else (section,)
    payload = await service.export(guild, with_docs=with_docs, only=only)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    await send_export(interaction, payload, filename=f"conductor-{guild.id}-{stamp}.json")


async def run_blueprint_apply(
    interaction: discord.Interaction,
    *,
    service: BlueprintCommands,
    attachment: discord.Attachment,
    mode: str | None,
    same_owner: SameOwner,
) -> None:
    guild = interaction.guild
    assert guild is not None
    blocked = attachment_error(size=int(attachment.size or 0), content_type=attachment.content_type)
    if blocked is not None:
        await respond(interaction, blueprint_rejected(blocked))
        return
    raw = await attachment.read()
    if len(raw) > BLUEPRINT_MAX_BYTES:
        await respond(interaction, blueprint_rejected("too_large"))
        return
    plan = await service.preview(guild, raw)
    if mode == "apply" and not plan.blocked:
        report = await service.apply(guild, plan, actor_id=interaction.user.id, wanted=wanted_from_bytes(raw))
        await respond(interaction, blueprint_done(report))
        return
    actor_id = interaction.user.id
    _PENDING[(int(guild.id), int(actor_id))] = raw

    async def yes(click: discord.Interaction) -> None:
        if not same_owner(click, actor_id):
            await respond(click, denied(copy.OWNER_DENIED, need=copy.OWNER_NEED))
            return
        if not await ack(click):
            return
        stored = _PENDING.pop((int(guild.id), int(actor_id)), raw)
        fresh = await service.preview(guild, stored)
        report = await service.apply(guild, fresh, actor_id=actor_id, wanted=wanted_from_bytes(stored))
        await InteractionTarget(click).edit(blueprint_done(report))

    async def no(click: discord.Interaction) -> None:
        if not same_owner(click, actor_id):
            await respond(click, denied(copy.OWNER_DENIED, need=copy.OWNER_NEED))
            return
        _PENDING.pop((int(guild.id), int(actor_id)), None)
        await update_message(click, ops_cancelled(copy.BLUEPRINT_TITLE))

    on_apply = None if plan.blocked else yes
    await respond(interaction, blueprint_plan_card(plan, on_apply=on_apply, on_cancel=no))
