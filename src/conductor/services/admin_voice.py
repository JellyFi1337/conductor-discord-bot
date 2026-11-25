"""Страница /admin «Голос»."""

from __future__ import annotations

from types import SimpleNamespace

from conductor.db.session import session_scope
from conductor.domain.access import VOICE_HUB_MANAGE, VOICE_PRESET
from conductor.domain.admin import (
    PAGE_VOICE,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyIssue,
    ReadyReport,
)
from conductor.domain.rooms import HubPreset, RoomDenial, parse_hub_settings
from conductor.presentation import copy


def _mention(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _guild(ctx: AdminCtx) -> object:
    return ctx.guild if ctx.guild is not None else SimpleNamespace(id=ctx.guild_id)


def _category_id(ctx: AdminCtx, channel_id: int) -> int | None:
    guild = ctx.guild
    getter = None if guild is None else getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    channel = getter(channel_id)
    raw = None if channel is None else getattr(channel, "category_id", None)
    return None if raw is None else int(raw)


async def _hub_row(ctx: AdminCtx):
    async with session_scope(ctx.bot.sessions) as session:
        return await ctx.bot.rooms.hub_for(session, ctx.guild_id)


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report, ready_issue

    guild = ctx.guild
    if guild is None:
        hub = await _hub_row(ctx)
        if hub is None:
            return pack_report([ready_issue("unbound", fix_control="voice.hub", skeleton_key="hub")])
        return pack_report(())
    denial, details = await ctx.bot.rooms.hub_health(guild)
    if denial is None:
        return pack_report(())
    return pack_report([_issue_for(denial, details)])


def _issue_for(denial: RoomDenial, details: dict[str, object]) -> ReadyIssue:
    from conductor.services.admin_pages import ready_issue

    if denial is RoomDenial.NO_HUB:
        return ready_issue("unbound", fix_control="voice.hub", skeleton_key="hub")
    if denial is RoomDenial.HUB_GONE:
        return ready_issue("channel_missing", fix_control="voice.fix", skeleton_key="hub")
    if denial is RoomDenial.NO_CATEGORY:
        return ready_issue("channel_missing", fix_control="voice.fix", skeleton_key="cat_rooms")
    if denial is RoomDenial.BOT_PERMS and details.get("target") == "verified":
        return ready_issue("member_perms", fix_control="voice.fix", skeleton_key="hub")
    return ready_issue("bot_perms", fix_control="voice.fix", skeleton_key="cat_rooms")


def _health_text(denial: RoomDenial | None) -> str:
    if denial is None:
        return copy.VOICE_HUB_HEALTH_OK
    return copy.voice_denial_text(denial.value)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    hub = await _hub_row(ctx)
    settings = parse_hub_settings("" if hub is None else hub.settings)
    preset = settings.preset or "—"
    from conductor.services.admin_pages import channel_pick_control, layout_channel_id

    parent_id = await layout_channel_id(ctx, "cat_rooms")
    if parent_id is None and hub is not None:
        parent_id = None if hub.category_id is None else int(hub.category_id)
    hub_id = None if hub is None else int(hub.channel_id)
    clone_id = settings.template_channel_id
    denial = None
    if ctx.guild is not None:
        denial, _details = await ctx.bot.rooms.hub_health(ctx.guild)
    controls = [
        await channel_pick_control(
            ctx,
            "voice.hub",
            copy.ADMIN_VOICE_HUB,
            parent_id=parent_id,
            channel_types=("voice",),
            current=None if hub_id is None else str(hub_id),
        ),
        PageControl(
            "select",
            "voice.preset",
            copy.ADMIN_VOICE_PRESET,
            options=(
                (copy.ADMIN_VOICE_NAME, HubPreset.NAME),
                (copy.ADMIN_VOICE_CLONE_OPT, HubPreset.CLONE),
            ),
            current=settings.preset or None,
        ),
        PageControl("button", "voice.template", copy.ADMIN_VOICE_TEMPLATE_BTN),
        await channel_pick_control(
            ctx,
            "voice.clone",
            copy.ADMIN_VOICE_CLONE,
            parent_id=parent_id,
            channel_types=("voice",),
            current=None if clone_id is None else str(int(clone_id)),
        ),
    ]
    if denial is not None:
        controls.append(PageControl("button", "voice.fix", copy.ADMIN_VOICE_FIX))
    return PageSnapshot(
        fields={
            copy.ADMIN_VOICE_HUB: _mention(None if hub is None else hub.channel_id),
            copy.ADMIN_VOICE_HEALTH: _health_text(denial),
            copy.ADMIN_VOICE_PRESET: preset,
            copy.ADMIN_VOICE_TEMPLATE: settings.template,
            copy.ADMIN_VOICE_CLONE: _mention(settings.template_channel_id),
        },
        controls=tuple(controls),
        footnote=copy.ADMIN_VOICE_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    rooms = ctx.bot.rooms
    if control_id == "voice.template":
        return AdminResult(ok=True, modal="text", target="voice.template.set")
    if control_id == "voice.fix":
        guild = ctx.guild
        if guild is None:
            return AdminResult(ok=False, error=copy.VOICE_NO_HUB)
        before, _details = await rooms.hub_health(guild)
        denial, _after = await rooms.repair_hub(guild, ctx.actor_id)
        return AdminResult(
            ok=denial is None,
            field=copy.ADMIN_VOICE_HEALTH,
            before=_health_text(before),
            after=_health_text(denial),
            error=None if denial is None else copy.voice_denial_text(denial.value),
        )
    if control_id == "voice.hub":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        current = await _hub_row(ctx)
        async with session_scope(ctx.bot.sessions) as session:
            if await rooms.is_room_channel(session, ctx.guild_id, channel_id):
                return AdminResult(ok=False, error=copy.VOICE_HUB_IS_ROOM)
            await rooms.set_hub(
                session,
                ctx.guild_id,
                channel_id=channel_id,
                category_id=_category_id(ctx, channel_id),
            )
        return AdminResult(
            ok=True,
            field=copy.ADMIN_VOICE_HUB,
            before=_mention(None if current is None else current.channel_id),
            after=_mention(channel_id),
        )
    hub = await _hub_row(ctx)
    if hub is None:
        return AdminResult(ok=False, error=copy.VOICE_NO_HUB)
    current = parse_hub_settings(hub.settings)
    if control_id == "voice.preset":
        if value == HubPreset.NAME:
            updated = await rooms.configure_preset(_guild(ctx), preset=HubPreset.NAME)
            if updated is None:
                return AdminResult(ok=False, error=copy.VOICE_NO_HUB)
            return AdminResult(
                ok=True,
                field=copy.ADMIN_VOICE_PRESET,
                before=current.preset or "—",
                after=updated.preset,
            )
        if value == HubPreset.CLONE:
            if current.template_channel_id is None:
                return AdminResult(ok=False, error=copy.ADMIN_VOICE_NEED_TEMPLATE)
            updated = await rooms.configure_preset(
                _guild(ctx),
                preset=HubPreset.CLONE,
                template_channel_id=current.template_channel_id,
            )
            if updated is None:
                return AdminResult(ok=False, error=copy.VOICE_MERGE_HUB)
            return AdminResult(
                ok=True,
                field=copy.ADMIN_VOICE_PRESET,
                before=current.preset or "—",
                after=updated.preset,
            )
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if control_id == "voice.clone":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        updated = await rooms.configure_preset(
            _guild(ctx),
            preset=HubPreset.CLONE,
            template=current.template,
            template_channel_id=channel_id,
        )
        if updated is None:
            return AdminResult(ok=False, error=copy.VOICE_MERGE_HUB)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_VOICE_CLONE,
            before=_mention(current.template_channel_id),
            after=_mention(channel_id),
        )
    if control_id == "voice.template.set":
        if "{n}" in value:
            return AdminResult(ok=False, error=copy.ADMIN_VOICE_TEMPLATE_N)
        preset = current.preset or HubPreset.NAME
        if preset == HubPreset.CLONE:
            if current.template_channel_id is None:
                return AdminResult(ok=False, error=copy.ADMIN_VOICE_NEED_TEMPLATE)
            updated = await rooms.configure_preset(
                _guild(ctx),
                preset=HubPreset.CLONE,
                template=value,
                template_channel_id=current.template_channel_id,
            )
        else:
            updated = await rooms.configure_preset(_guild(ctx), preset=HubPreset.NAME, template=value)
        if updated is None:
            return AdminResult(ok=False, error=copy.VOICE_NO_HUB)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_VOICE_TEMPLATE,
            before=current.template,
            after=updated.template,
        )
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_VOICE,
    copy.ADMIN_PAGE_VOICE,
    VOICE_HUB_MANAGE,
    _ready,
    _snapshot,
    _handle,
    keys={
        "voice.hub": VOICE_HUB_MANAGE,
        "voice.fix": VOICE_HUB_MANAGE,
        "voice.preset": VOICE_PRESET,
        "voice.template": VOICE_PRESET,
        "voice.clone": VOICE_PRESET,
    },
)
