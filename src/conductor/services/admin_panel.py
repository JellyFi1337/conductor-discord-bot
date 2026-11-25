"""Склейка реестра страниц /admin с эфемерными карточками."""

from __future__ import annotations

import discord
from discord import ui

from conductor.domain.access import AccessDecision, AccessSnapshot
from conductor.domain.admin import AdminCtx, AdminPage, AdminResult, ReadyIssue, ReadyState
from conductor.presentation import copy
from conductor.presentation.admin import (
    CountReasonModal,
    DurationModal,
    IdsModal,
    KeyRanksModal,
    LinesModal,
    NumberModal,
    NumbersModal,
    TextModal,
    confirm_card,
    home_card,
    issues_card,
    no_pages_card,
    page_card,
    send_export,
)
from conductor.presentation.icons import render_icon
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, edit_response, error_code, respond, update_message
from conductor.presentation.motion import InteractionTarget, Progress, StageState
from conductor.presentation.self_roles import SelfRoleOptionModal
from conductor.presentation.tickets import FormQuestionsModal
from conductor.presentation.tokens import Icon
from conductor.presentation.youtube import YoutubeFeedModal
from conductor.services.access import cached_snapshot
from conductor.services.admin_pages import (
    all_pages,
    filter_controls,
    hidden_footnote,
    log_change,
    may_use,
    page_by_id,
    page_visible,
    pages_for,
)
from conductor.services.history import IngestTick, ingest_guild, skip_ids_from_config


async def open_panel(ctx: AdminCtx) -> ui.LayoutView | None:
    if not await _visible(ctx):
        return None
    return await render_home(ctx)


async def render_home(ctx: AdminCtx) -> ui.LayoutView:
    pages = await _visible(ctx)
    fields = {page.label: await _status(page, ctx) for page in pages}

    async def on_page(interaction: discord.Interaction, page_id: str) -> None:
        chosen = page_by_id(page_id)
        if chosen is None or not await _page_open(ctx, chosen):
            await respond(interaction, no_pages_card())
            return
        await update_message(interaction, await render_page(ctx, chosen))

    async def on_issues(interaction: discord.Interaction) -> None:
        await update_message(interaction, await render_issues(ctx))

    return home_card(
        fields=fields,
        pages=tuple((page.id, page.label) for page in pages),
        on_page=on_page,
        on_issues=on_issues,
    )


async def render_issues(ctx: AdminCtx) -> ui.LayoutView:
    pages = await _visible(ctx)
    groups: list[tuple[str, str, tuple[ReadyIssue, ...]]] = []
    for page in pages:
        report = await page.ready(ctx)
        if not report.issues:
            continue
        groups.append((page.id, page.label, report.issues))

    async def on_fix(interaction: discord.Interaction, skeleton_key: str) -> None:
        if not await ack(interaction):
            return
        guild = ctx.guild
        seed = getattr(ctx.bot, "seed", None)
        repair = None if seed is None else getattr(seed, "repair", None)
        if guild is None or not callable(repair):
            await _redraw(interaction, await render_issues(ctx), via_modal=True)
            return
        await repair(guild, skeleton_key, actor_id=ctx.actor_id)
        await _redraw(interaction, await render_issues(ctx), via_modal=True)

    async def on_open(interaction: discord.Interaction, page_id: str) -> None:
        chosen = page_by_id(page_id)
        if chosen is None or not await _page_open(ctx, chosen):
            await respond(interaction, no_pages_card())
            return
        await update_message(interaction, await render_page(ctx, chosen))

    async def on_home(interaction: discord.Interaction) -> None:
        await update_message(interaction, await render_home(ctx))

    return issues_card(groups=groups, on_fix=on_fix, on_open=on_open, on_home=on_home)


async def render_page(ctx: AdminCtx, page: AdminPage, *, leaf: int = 1) -> ui.LayoutView:
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    if not _visible_page(snapshot, ctx, page):
        return no_pages_card()
    snap = await page.snapshot(ctx, leaf)
    controls = filter_controls(
        snapshot,
        page,
        snap.controls,
        member_role_ids=ctx.member_role_ids,
        is_administrator=ctx.is_administrator,
        is_developer=ctx.is_developer,
        suspended=ctx.suspended,
    )
    if not controls.allowed and controls.hidden:
        need = min((rank for _, rank in controls.hidden if rank is not None), default=None)
        rank_key = need.key if need is not None else "special"
        return denied(copy.denied_actors(rank_key), need=copy.rank_label(rank_key))
    footnote = snap.footnote
    hidden_line = hidden_footnote(controls.hidden)
    if hidden_line:
        footnote = f"{footnote}\n{hidden_line}" if footnote else hidden_line

    async def on_control(interaction: discord.Interaction, control_id: str, value: str) -> None:
        await apply_control(interaction, ctx, page, control_id, value)

    async def on_home(interaction: discord.Interaction) -> None:
        await update_message(interaction, await render_home(ctx))

    return page_card(
        title=page.label,
        fields=snap.fields,
        footnote=footnote,
        controls=controls.allowed,
        on_control=on_control,
        on_home=on_home,
    )


def _opens_modal(control_id: str) -> bool:
    """Кнопки и селекты, которые только открывают модалку: ответ интеракции нельзя тратить на defer."""
    if control_id.endswith(".set") or ".set:" in control_id:
        return False
    if control_id in {
        "general.other",
        "logs.keep",
        "mute.evidence_hours",
        "rep.hits",
        "rep.growth",
        "rep.names",
        "rep.appeal",
        "ladder.regular_group",
        "ladder.resident_group",
        "ladder.friend_group",
        "voice.template",
        "data.ingest",
        "tickets.add",
        "lfg.numbers",
        "alerts.floors",
        "give.weights",
        "sr.add",
        "yt.add",
    }:
        return True
    return control_id.startswith(
        ("perms.edit", "tickets.q:", "tickets.h:", "tickets.i:", "quota.", "achieve.count:", "achieve.names:")
    )


async def apply_control(
    interaction: discord.Interaction,
    ctx: AdminCtx,
    page: AdminPage,
    control_id: str,
    value: str,
    *,
    via_modal: bool = False,
) -> None:
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    decision = may_use(
        snapshot,
        page,
        control_id,
        member_role_ids=ctx.member_role_ids,
        is_administrator=ctx.is_administrator,
        is_developer=ctx.is_developer,
        suspended=ctx.suspended,
    )
    if not decision.allowed:
        await respond(interaction, _denied_key(decision))
        return
    if not via_modal and not _opens_modal(control_id) and control_id != "skel.map" and not await ack(interaction):
        return
    result = await page.handle(ctx, control_id, value)
    if result.modal:
        await _send_modal(interaction, ctx, page, result)
        return
    if via_modal and not interaction.response.is_done() and not await ack(interaction):
        return
    if result.confirm:
        await _send_confirm(interaction, ctx, page, result, value)
        return
    if result.progress == "ingest":
        await _run_ingest(interaction, ctx, page, result)
        return
    if result.progress == "skeleton":
        await _run_skeleton_build(interaction, ctx, page)
        return
    if result.progress == "setup_map":
        await _open_setup_map(interaction, ctx)
        return
    if result.file is not None:
        await send_export(interaction, result.file, filename=result.filename or "permissions.yml")
        return
    if not result.ok:
        await respond(interaction, denied(result.error or copy.CONFIG_DENIED))
        return
    if result.field:
        log_change(
            ctx.bot,
            ctx.guild_id,
            page=page.label,
            field=result.field,
            before=result.before or "—",
            after=result.after or "—",
            actor_id=ctx.actor_id,
        )
    leaf = result.leaf if result.leaf is not None else 1
    await _redraw(interaction, await render_page(ctx, page, leaf=leaf), via_modal=via_modal)


async def _visible(ctx: AdminCtx) -> tuple[AdminPage, ...]:
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    found = pages_for(
        snapshot,
        member_role_ids=ctx.member_role_ids,
        is_administrator=ctx.is_administrator,
        is_developer=ctx.is_developer,
        suspended=ctx.suspended,
    )
    return found or all_pages()[:0]


async def _page_open(ctx: AdminCtx, page: AdminPage) -> bool:
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    return _visible_page(snapshot, ctx, page)


def _visible_page(snapshot: AccessSnapshot, ctx: AdminCtx, page: AdminPage) -> bool:
    return page_visible(
        snapshot,
        page,
        member_role_ids=ctx.member_role_ids,
        is_administrator=ctx.is_administrator,
        is_developer=ctx.is_developer,
        suspended=ctx.suspended,
    )


def _denied_key(decision: AccessDecision) -> ui.LayoutView:
    if decision.deny_copy:
        return denied(decision.deny_copy)
    if decision.need is not None:
        return denied(copy.denied_actors(decision.need.key), need=copy.rank_label(decision.need.key))
    return denied(copy.CONFIG_DENIED)


_READY_ICONS = {
    ReadyState.READY: Icon.DONE,
    ReadyState.PARTIAL: Icon.WARN,
    ReadyState.BLOCKED: Icon.DENY,
    ReadyState.OFF: Icon.LOCK,
}


async def _status(page: AdminPage, ctx: AdminCtx) -> str:
    report = await page.ready(ctx)
    reason = report.issues[0].label if report.issues else ""
    mark = render_icon(_READY_ICONS.get(report.state, Icon.DENY))
    text = copy.admin_status_text(report.state.value, reason)
    return f"{mark} {text}".strip()


async def _send_modal(
    interaction: discord.Interaction,
    ctx: AdminCtx,
    page: AdminPage,
    result: AdminResult,
) -> None:
    target = result.target or ""

    async def done(submit: discord.Interaction, raw: str, control_id: str = target) -> None:
        await apply_control(submit, ctx, page, control_id, raw, via_modal=True)

    kind = result.modal
    if kind == "number":
        modal: ui.Modal = NumberModal(lo=result.lo or 0, hi=result.hi or 0, on_done=done)
    elif kind == "numbers":
        modal = NumbersModal(
            title=result.title or copy.ADMIN_MODAL_NUMBER,
            labels=result.labels,
            defaults=result.defaults,
            on_done=done,
        )
    elif kind == "ids":
        modal = IdsModal(on_done=done)
    elif kind == "duration":
        modal = DurationModal(on_done=done)
    elif kind == "key_ranks":
        modal = KeyRanksModal(on_done=done)
    elif kind == "form_questions":
        modal = FormQuestionsModal(result.title or "", existing=result.defaults, on_done=done)
    elif kind == "self_role":
        modal = SelfRoleOptionModal(on_done=done, defaults=result.defaults)
    elif kind == "youtube":
        modal = YoutubeFeedModal(on_done=done, defaults=result.defaults)
    elif kind == "lines":
        modal = LinesModal(
            title=result.title or copy.ADMIN_MODAL_TEXT,
            label=result.labels[0] if result.labels else copy.ADMIN_LABEL_TEXT,
            default=result.defaults[0] if result.defaults else "",
            on_done=done,
        )
    elif kind == "achieve_count":
        modal = CountReasonModal(
            title=result.title or copy.ADMIN_ACHIEVE_COUNT_BTN,
            labels=result.labels,
            defaults=result.defaults,
            on_done=done,
        )
    else:
        modal = TextModal(on_done=done)
    await interaction.response.send_modal(modal)


async def _send_confirm(
    interaction: discord.Interaction,
    ctx: AdminCtx,
    page: AdminPage,
    result: AdminResult,
    value: str,
) -> None:
    payload = result.after or value
    yes_id = result.confirm or ""

    async def yes(click: discord.Interaction) -> None:
        await apply_control(click, ctx, page, yes_id, payload)

    async def no(click: discord.Interaction) -> None:
        await update_message(click, await render_page(ctx, page))

    view = confirm_card(
        question=result.title or copy.DATA_FORGET_CONFIRM,
        verb=(result.labels[0] if result.labels else copy.DATA_FORGET_VERB),
        on_yes=yes,
        on_no=no,
    )
    if interaction.response.is_done():
        await edit_response(interaction, view)
        return
    await update_message(interaction, view)


async def _run_ingest(
    interaction: discord.Interaction,
    ctx: AdminCtx,
    page: AdminPage,
    result: AdminResult,
) -> None:
    guild = ctx.guild
    if guild is None:
        await respond(interaction, denied(copy.ADMIN_NEED_GUILD))
        return
    busy = getattr(ctx.bot, "history_busy", None)
    if not isinstance(busy, set):
        await respond(interaction, denied(copy.INGEST_BUSY_BODY))
        return
    if int(guild.id) in busy:
        await respond(interaction, denied(copy.INGEST_BUSY_BODY))
        return
    parts = (result.after or "0 1").split()
    per_channel = int(parts[0]) if parts else 0
    reactions = True if len(parts) < 2 else parts[1] != "0"
    busy.add(int(guild.id))
    progress = Progress(
        InteractionTarget(interaction),
        title=copy.INGEST_TITLE,
        body=copy.INGEST_BODY,
        footnote=copy.INGEST_FOOTNOTE,
        unit=copy.INGEST_UNIT,
        registry=getattr(ctx.bot, "motion", None),
        code=error_code(interaction),
    )

    async def on_progress(tick: IngestTick) -> None:
        await progress.step(
            tick.done,
            note=copy.ingest_now(tick.channel),
            total=tick.total if tick.total > 0 else None,
            messages=tick.messages,
        )

    try:
        async with progress:
            await progress.start()
            report = await ingest_guild(
                guild,
                sessions=ctx.bot.sessions,
                collector=ctx.bot.collector,
                ledger=ctx.bot.history,
                skip_channel_ids=skip_ids_from_config(ctx.bot.config_by_guild.get(guild.id)),
                per_channel=None if per_channel <= 0 else per_channel,
                with_reactions=reactions,
                on_progress=on_progress,
            )
            summary = copy.ingest_done_body(
                messages=report.messages,
                channels=report.channels,
                authors=report.authors,
                ties=report.ties,
                skipped=report.skipped,
                reactions=report.reactions,
                reset=report.reset,
                timed_out=report.timed_out,
            )
            log_change(
                ctx.bot,
                ctx.guild_id,
                page=page.label,
                field=copy.ADMIN_DATA_INGEST,
                before="—",
                after=summary,
                actor_id=ctx.actor_id,
            )
            await progress.finish(await render_page(ctx, page))
    finally:
        busy.discard(int(guild.id))


async def _run_skeleton_build(interaction: discord.Interaction, ctx: AdminCtx, page: AdminPage) -> None:
    from conductor.services.admin_skeleton import commit_build

    progress = Progress(
        InteractionTarget(interaction),
        title=copy.SKELETON_BUILD_TITLE,
        body=copy.SKELETON_BUILD,
        registry=getattr(ctx.bot, "motion", None),
        code=error_code(interaction),
    )
    labels = tuple(copy.SKELETON_BUILD_STAGES)

    async def on_progress(name: str, ok: bool) -> None:
        mapped = _skeleton_stage(name)
        if mapped is None:
            return
        await progress.stage(mapped, StageState.DONE if ok else StageState.FAIL)

    async with progress:
        await progress.start()
        for label in labels:
            await progress.stage(label, StageState.PENDING)
        result = await commit_build(ctx, on_progress=on_progress)
        if not result.ok:
            await progress.fail(error_code(interaction))
            await respond(interaction, denied(result.error or copy.CONFIG_DENIED))
            return
        await progress.finish(await render_page(ctx, page, leaf=result.leaf or 1))


def _skeleton_stage(name: str) -> str | None:
    stages = tuple(copy.SKELETON_BUILD_STAGES)
    seed_stages = tuple(copy.OWNER_SEED_STAGES)
    if name in stages:
        return name
    if seed_stages and name == seed_stages[0]:
        return stages[0]
    if len(seed_stages) > 1 and name == seed_stages[1]:
        return stages[1]
    if len(seed_stages) > 2 and name == seed_stages[2]:
        return stages[2]
    if len(seed_stages) > 4 and name == seed_stages[4]:
        return stages[3]
    return None


async def _open_setup_map(interaction: discord.Interaction, ctx: AdminCtx) -> None:
    opener = getattr(getattr(ctx.bot, "setup_map", None), "opener", None)
    if not callable(opener):
        await respond(interaction, denied(copy.CONFIG_UNKNOWN_KEY))
        return
    await opener(actor_id=int(ctx.actor_id))(interaction)


async def _redraw(interaction: discord.Interaction, view: ui.LayoutView, *, via_modal: bool) -> None:
    if via_modal:
        if not interaction.response.is_done():
            await interaction.response.defer()
        await edit_response(interaction, view)
        return
    if interaction.response.is_done():
        await edit_response(interaction, view)
        return
    await update_message(interaction, view)
