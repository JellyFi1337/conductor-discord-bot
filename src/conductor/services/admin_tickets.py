"""Страница /admin «Тикеты»."""

from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy import select

from conductor.db.models import TicketPanel
from conductor.db.session import session_scope
from conductor.domain.access import STAFF_RANKS, TICKETS_FORM_MANAGE, TICKETS_PANEL_POST
from conductor.domain.admin import (
    PAGE_TICKETS,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.domain.tickets import (
    ApplicationTopic,
    CategoryKind,
    StoredCategory,
    TicketOpenMode,
    parse_kind,
    parse_ticket_settings,
    visible_rank_set,
)
from conductor.presentation import copy


@dataclass(frozen=True, slots=True)
class _Draft:
    channel_id: int | None = None
    archive_id: int | None = None
    category_id: int | None = None
    log_id: int | None = None
    mode: str = TicketOpenMode.CHANNEL
    author_close: bool = False
    picked: str = ""


_DRAFTS: dict[int, _Draft] = {}

_KIND_OPTIONS = tuple((copy.ticket_kind_label(kind), kind) for kind in CategoryKind)
_TOPIC_OPTIONS = tuple((copy.ticket_topic_label(topic), topic) for topic in ApplicationTopic)
_RANK_OPTIONS = tuple((copy.rank_label(rank.key), rank.key) for rank in STAFF_RANKS)


def _mention(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _draft(guild_id: int) -> _Draft:
    return _DRAFTS.get(int(guild_id), _Draft())


def _store(guild_id: int, draft: _Draft) -> None:
    _DRAFTS[int(guild_id)] = draft


def _channel(guild: object | None, channel_id: int | None):
    if guild is None or channel_id is None:
        return None
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    return getter(int(channel_id))


async def _panel_row(ctx: AdminCtx) -> TicketPanel | None:
    async with session_scope(ctx.bot.sessions) as session:
        return await session.scalar(
            select(TicketPanel).where(TicketPanel.guild_id == ctx.guild_id).order_by(TicketPanel.id.desc())
        )


async def _categories(ctx: AdminCtx) -> list[StoredCategory]:
    listed = getattr(ctx.bot.tickets, "list_categories", None)
    if not callable(listed):
        return []
    return list(await listed(ctx.guild_id))


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import (
        channel_issue,
        member_access_issue,
        pack_report,
        ready_issue,
        verified_roles,
    )

    row = await _panel_row(ctx)
    if row is None:
        return pack_report([ready_issue("unbound", fix_control="tickets.channel", skeleton_key="tickets")])
    issues = [
        channel_issue(ctx.guild, int(row.channel_id), fix_control="tickets.channel", skeleton_key="tickets"),
    ]
    if int(row.message_id or 0) <= 0:
        issues.append(ready_issue("panel_stale", fix_control="tickets.post", skeleton_key="tickets"))
    issues.append(
        member_access_issue(
            ctx.guild,
            int(row.channel_id),
            roles=await verified_roles(ctx),
            need=("view_channel",),
            skeleton_key="tickets",
            fix_control="tickets.channel",
        )
    )
    return pack_report(issues)


def _picked(draft: _Draft, categories: list[StoredCategory]) -> StoredCategory | None:
    if not categories:
        return None
    for item in categories:
        if item.name == draft.picked:
            return item
    return categories[0]


def _category_fields(item: StoredCategory) -> dict[str, object]:
    ranks = visible_rank_set(item.visible_ranks, item.kind)
    return {
        copy.ADMIN_TICKET_KIND: copy.ticket_kind_label(parse_kind(item.kind)),
        copy.ADMIN_TICKET_RANKS: copy.ticket_visible_ranks_value(item.visible_ranks) if ranks else "—",
        copy.ADMIN_TICKET_TOPIC: copy.ticket_topic_label(item.topic) if item.topic else "—",
        copy.ADMIN_TICKET_INFO: item.info_text or "—",
    }


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    draft = _draft(ctx.guild_id)
    row = await _panel_row(ctx)
    if row is not None:
        settings = parse_ticket_settings(row.settings)
        if draft.channel_id is None:
            draft = replace(
                draft,
                channel_id=row.channel_id,
                archive_id=settings.archive_channel_id,
                category_id=settings.discord_category_id,
                log_id=settings.log_channel_id,
                mode=settings.open_mode or TicketOpenMode.CHANNEL,
                author_close=bool(settings.author_may_close),
            )
            _store(ctx.guild_id, draft)
    categories = await _categories(ctx)
    chosen = _picked(draft, categories)
    if chosen is not None and draft.picked != chosen.name:
        draft = replace(draft, picked=chosen.name)
        _store(ctx.guild_id, draft)
    fields: dict[str, object] = {
        copy.ADMIN_TICKET_CHANNEL: _mention(draft.channel_id),
        copy.ADMIN_TICKET_ARCHIVE: _mention(draft.archive_id),
        copy.ADMIN_TICKET_LOG: _mention(draft.log_id),
        copy.ADMIN_TICKET_CATEGORY: _mention(draft.category_id),
        copy.ADMIN_TICKET_MODE: draft.mode,
        copy.ADMIN_TICKET_AUTHOR_CLOSE: copy.yn(draft.author_close),
        copy.ADMIN_TICKET_FORMS: ", ".join(item.name for item in categories) or "—",
    }
    for item in categories:
        help_ = await ctx.bot.tickets.category_help(ctx.guild_id, item.name)
        if help_ is None:
            continue
        hint, form = help_
        fields[item.name] = copy.ticket_questions_value([field.prompt for field in form.fields])
        if hint:
            fields[f"{item.name} · {copy.TICKET_FIELD_HINT}"] = hint
    if chosen is not None:
        fields.update(_category_fields(chosen))
    controls: list[PageControl] = [
        PageControl("channel", "tickets.channel", copy.ADMIN_TICKET_CHANNEL, channel_types=("text",)),
        PageControl("channel", "tickets.archive", copy.ADMIN_TICKET_ARCHIVE, channel_types=("text",)),
        PageControl("channel", "tickets.log", copy.ADMIN_TICKET_LOG, channel_types=("text",)),
        PageControl("channel", "tickets.category", copy.ADMIN_TICKET_CATEGORY, channel_types=("category",)),
        PageControl(
            "select",
            "tickets.mode",
            copy.ADMIN_TICKET_MODE,
            options=(
                (copy.ADMIN_TICKET_MODE_CHANNEL, TicketOpenMode.CHANNEL),
                (copy.ADMIN_TICKET_MODE_THREAD, TicketOpenMode.THREAD),
            ),
            current=draft.mode,
        ),
        PageControl(
            "button",
            "tickets.close",
            copy.admin_toggle(draft.author_close, copy.ADMIN_TICKET_CLOSE_SUBJECT),
        ),
        PageControl("button", "tickets.post", copy.ADMIN_TICKET_POST),
        PageControl("button", "tickets.add", copy.ADMIN_TICKET_ADD),
    ]
    if categories:
        controls.append(
            PageControl(
                "select",
                "tickets.pick",
                copy.ADMIN_TICKET_PICK,
                options=tuple((item.name, item.name) for item in categories),
                current=None if chosen is None else chosen.name,
            )
        )
    if chosen is not None:
        name = chosen.name
        controls.extend(
            (
                PageControl(
                    "select",
                    f"tickets.kind:{name}",
                    copy.ADMIN_TICKET_KIND,
                    options=_KIND_OPTIONS,
                    current=parse_kind(chosen.kind),
                ),
                PageControl(
                    "select",
                    f"tickets.vis:{name}",
                    copy.ADMIN_TICKET_RANKS,
                    options=_RANK_OPTIONS,
                    current=",".join(
                        rank.key for rank in STAFF_RANKS if rank in visible_rank_set(chosen.visible_ranks, chosen.kind)
                    ),
                    min_values=1,
                    max_values=len(STAFF_RANKS),
                ),
                PageControl(
                    "select",
                    f"tickets.topic:{name}",
                    copy.ADMIN_TICKET_TOPIC,
                    options=_TOPIC_OPTIONS,
                    current=chosen.topic or None,
                    min_values=0,
                    max_values=1,
                ),
                PageControl("button", f"tickets.q:{name}", copy.admin_ticket_questions(name)),
                PageControl("button", f"tickets.h:{name}", copy.admin_ticket_hint(name)),
                PageControl("button", f"tickets.i:{name}", copy.admin_ticket_info(name)),
                PageControl("button", f"tickets.c:{name}", copy.admin_ticket_clear(name)),
                PageControl("button", f"tickets.rm:{name}", copy.admin_ticket_remove(name)),
            )
        )
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.ADMIN_TICKET_HINT)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    draft = _draft(ctx.guild_id)
    if control_id == "tickets.channel":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        _store(ctx.guild_id, replace(draft, channel_id=channel_id))
        return AdminResult(
            ok=True,
            field=copy.ADMIN_TICKET_CHANNEL,
            before=_mention(draft.channel_id),
            after=_mention(channel_id),
        )
    if control_id == "tickets.archive":
        try:
            archive_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        _store(ctx.guild_id, replace(draft, archive_id=archive_id))
        return AdminResult(
            ok=True,
            field=copy.ADMIN_TICKET_ARCHIVE,
            before=_mention(draft.archive_id),
            after=_mention(archive_id),
        )
    if control_id == "tickets.log":
        try:
            log_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        _store(ctx.guild_id, replace(draft, log_id=log_id))
        setter = getattr(ctx.bot.tickets, "set_log_channel", None)
        if callable(setter):
            await setter(ctx.guild_id, log_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_TICKET_LOG,
            before=_mention(draft.log_id),
            after=_mention(log_id),
        )
    if control_id == "tickets.category":
        try:
            category_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        _store(ctx.guild_id, replace(draft, category_id=category_id))
        return AdminResult(
            ok=True,
            field=copy.ADMIN_TICKET_CATEGORY,
            before=_mention(draft.category_id),
            after=_mention(category_id),
        )
    if control_id == "tickets.mode":
        mode = value if value in {TicketOpenMode.CHANNEL, TicketOpenMode.THREAD} else TicketOpenMode.CHANNEL
        _store(ctx.guild_id, replace(draft, mode=mode))
        return AdminResult(ok=True, field=copy.ADMIN_TICKET_MODE, before=draft.mode, after=mode)
    if control_id == "tickets.close":
        nxt = not draft.author_close
        _store(ctx.guild_id, replace(draft, author_close=nxt))
        return AdminResult(
            ok=True,
            field=copy.ADMIN_TICKET_AUTHOR_CLOSE,
            before=copy.yn(draft.author_close),
            after=copy.yn(nxt),
        )
    if control_id == "tickets.post":
        return await _post(ctx, draft)
    if control_id == "tickets.pick":
        _store(ctx.guild_id, replace(draft, picked=value))
        return AdminResult(ok=True, field=copy.ADMIN_TICKET_PICK, before=draft.picked or "—", after=value or "—")
    if control_id == "tickets.add":
        return AdminResult(ok=True, modal="text", target="tickets.add.set")
    if control_id == "tickets.add.set":
        added = await ctx.bot.tickets.add_category(ctx.guild_id, value)
        if not added:
            return AdminResult(ok=False, error=copy.ADMIN_TICKET_DUP if value.strip() else copy.ADMIN_TICKET_BAD_NAME)
        _store(ctx.guild_id, replace(draft, picked=value.strip()[:80]))
        return AdminResult(ok=True, field=copy.ADMIN_TICKET_ADD, before="—", after=value.strip()[:80])
    if control_id.startswith("tickets.kind:"):
        name = control_id.removeprefix("tickets.kind:")
        updated = await ctx.bot.tickets.set_category_kind(ctx.guild_id, name, value)
        if not updated:
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        return AdminResult(ok=True, field=copy.ADMIN_TICKET_KIND, before="—", after=copy.ticket_kind_label(value))
    if control_id.startswith("tickets.vis:"):
        name = control_id.removeprefix("tickets.vis:")
        updated = await ctx.bot.tickets.set_category_ranks(ctx.guild_id, name, value)
        if not updated:
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_TICKET_RANKS,
            before="—",
            after=copy.ticket_visible_ranks_value(value),
        )
    if control_id.startswith("tickets.topic:"):
        name = control_id.removeprefix("tickets.topic:")
        updated = await ctx.bot.tickets.set_category_topic(ctx.guild_id, name, value)
        if not updated:
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        return AdminResult(ok=True, field=copy.ADMIN_TICKET_TOPIC, before="—", after=copy.ticket_topic_label(value))
    if control_id.startswith("tickets.q:"):
        name = control_id.removeprefix("tickets.q:")
        help_ = await ctx.bot.tickets.category_help(ctx.guild_id, name)
        if help_ is None:
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        _hint, form = help_
        return AdminResult(
            ok=True,
            modal="form_questions",
            target="tickets.form.saved",
            title=name,
            defaults=tuple(field.prompt for field in form.fields),
        )
    if control_id.startswith("tickets.h:"):
        name = control_id.removeprefix("tickets.h:")
        return AdminResult(ok=True, modal="text", target=f"tickets.hint:{name}")
    if control_id.startswith("tickets.i:"):
        name = control_id.removeprefix("tickets.i:")
        return AdminResult(ok=True, modal="text", target=f"tickets.info:{name}")
    if control_id.startswith("tickets.c:"):
        name = control_id.removeprefix("tickets.c:")
        updated = await ctx.bot.tickets.clear_category_help(ctx.guild_id, name)
        if not updated:
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        return AdminResult(ok=True, field=copy.admin_ticket_clear(name), before="—", after=copy.TICKET_FORM_CLEARED)
    if control_id.startswith("tickets.rm:"):
        name = control_id.removeprefix("tickets.rm:")
        result = await ctx.bot.tickets.remove_category(ctx.guild_id, name)
        if result == "missing":
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        if result == "open":
            return AdminResult(ok=False, error=copy.ADMIN_TICKET_HAS_OPEN)
        if draft.picked == name:
            _store(ctx.guild_id, replace(draft, picked=""))
        return AdminResult(ok=True, field=copy.admin_ticket_remove(name), before=name, after="—")
    if control_id.startswith("tickets.hint:"):
        name = control_id.removeprefix("tickets.hint:")
        hint = "" if value.strip() in {"-", "—"} else value
        updated = await ctx.bot.tickets.set_category_hint(ctx.guild_id, name, hint)
        if not updated:
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        return AdminResult(ok=True, field=copy.admin_ticket_hint(name), before="—", after=hint or "—")
    if control_id.startswith("tickets.info:"):
        name = control_id.removeprefix("tickets.info:")
        info = "" if value.strip() in {"-", "—"} else value
        updated = await ctx.bot.tickets.set_category_info(ctx.guild_id, name, info)
        if not updated:
            return AdminResult(ok=False, error=copy.TICKET_NO_CATEGORY)
        return AdminResult(ok=True, field=copy.admin_ticket_info(name), before="—", after=info or "—")
    if control_id == "tickets.form.saved":
        return AdminResult(ok=True, field=copy.TICKET_FIELD_QUESTIONS, before="—", after=value or "—")
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


async def _post(ctx: AdminCtx, draft: _Draft) -> AdminResult:
    channel = _channel(ctx.guild, draft.channel_id)
    archive = _channel(ctx.guild, draft.archive_id)
    if channel is None or archive is None:
        return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
    category = _channel(ctx.guild, draft.category_id)
    posted = await ctx.bot.tickets.post_panel(
        ctx.guild,
        channel=channel,
        archive=archive,
        actor_id=ctx.actor_id,
        category=category,
        mode=draft.mode,
        author_may_close=draft.author_close,
        log_channel_id=draft.log_id,
    )
    if not posted:
        return AdminResult(ok=False, error=copy.STALE_BODY)
    return AdminResult(ok=True, field=copy.ADMIN_TICKET_POST, before="—", after=_mention(draft.channel_id))


_PANEL_CONTROLS = (
    "tickets.channel",
    "tickets.archive",
    "tickets.log",
    "tickets.category",
    "tickets.mode",
    "tickets.close",
    "tickets.post",
)
_FORM_CONTROLS = (
    "tickets.add",
    "tickets.pick",
    "tickets.kind",
    "tickets.vis",
    "tickets.topic",
    "tickets.q",
    "tickets.h",
    "tickets.i",
    "tickets.c",
    "tickets.rm",
    "tickets.hint",
    "tickets.info",
    "tickets.form",
)

PAGE = AdminPage(
    PAGE_TICKETS,
    copy.ADMIN_PAGE_TICKETS,
    TICKETS_PANEL_POST,
    _ready,
    _snapshot,
    _handle,
    keys={
        **dict.fromkeys(_PANEL_CONTROLS, TICKETS_PANEL_POST),
        **dict.fromkeys(_FORM_CONTROLS, TICKETS_FORM_MANAGE),
    },
)
