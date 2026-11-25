"""Страница /admin «YouTube»."""

from __future__ import annotations

from dataclasses import dataclass, replace

from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_YOUTUBE,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.domain.youtube import DEFAULT_TEMPLATE, valid_channel_id
from conductor.presentation import copy

_DRAFTS: dict[int, _Draft] = {}


@dataclass(frozen=True, slots=True)
class _Draft:
    yt_channel_id: str | None = None
    template: str = DEFAULT_TEMPLATE
    selected_id: int | None = None


def _draft(guild_id: int) -> _Draft:
    return _DRAFTS.get(int(guild_id), _Draft())


def _store(guild_id: int, draft: _Draft) -> None:
    _DRAFTS[int(guild_id)] = draft


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _mention_role(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "—"


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import channel_issue, pack_report

    service = getattr(ctx.bot, "youtube", None)
    listing = getattr(service, "feeds", None)
    if not callable(listing):
        return pack_report((), off=True)
    feeds = await listing(ctx.guild_id)
    bound = [item for item in feeds if item.discord_channel_id]
    if not bound:
        return pack_report((), off=True)
    return pack_report(
        [channel_issue(ctx.guild, int(item.discord_channel_id), fix_control="yt.channel") for item in bound],
        optional_key="video",
    )


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    service = ctx.bot.youtube
    feeds = await service.feeds(ctx.guild_id)
    draft = _draft(ctx.guild_id)
    chosen = next((item for item in feeds if int(item.id) == int(draft.selected_id or 0)), None)
    if chosen is None and feeds:
        chosen = feeds[0]
        _store(ctx.guild_id, replace(draft, selected_id=int(chosen.id)))
        draft = _draft(ctx.guild_id)
    pending = draft.yt_channel_id or "—"
    fields = {
        copy.ADMIN_YT_FEEDS: _feed_lines(feeds),
        copy.ADMIN_YT_ADD: pending,
        copy.ADMIN_YT_CHANNEL: _mention_channel(None if chosen is None else chosen.discord_channel_id),
        copy.ADMIN_YT_ROLE: _mention_role(None if chosen is None else chosen.ping_role_id),
    }
    controls: list[PageControl] = [
        PageControl("button", "yt.add", copy.ADMIN_YT_ADD),
        PageControl("channel", "yt.channel", copy.ADMIN_YT_CHANNEL, channel_types=("text",)),
        PageControl("role", "yt.role", copy.ADMIN_YT_ROLE, min_values=0, max_values=1),
    ]
    if feeds:
        controls.append(
            PageControl(
                "select",
                "yt.pick",
                copy.ADMIN_YT_FEEDS,
                options=tuple((_feed_label(item), str(item.id)) for item in feeds),
                current=None if chosen is None else str(chosen.id),
            )
        )
        controls.append(PageControl("button", "yt.remove", copy.ADMIN_YT_REMOVE))
        controls.append(PageControl("button", "yt.check", copy.ADMIN_YT_CHECK))
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.ADMIN_YT_HINT)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    service = getattr(ctx.bot, "youtube", None)
    if service is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    draft = _draft(ctx.guild_id)
    if control_id == "yt.add":
        return AdminResult(
            ok=True,
            modal="youtube",
            target="yt.add.set",
            defaults=(draft.yt_channel_id or "", draft.template),
        )
    if control_id == "yt.add.set":
        channel_id, _, template = value.partition("\n")
        parsed = valid_channel_id(channel_id)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_YT_BAD_ID)
        _store(ctx.guild_id, replace(draft, yt_channel_id=parsed, template=template.strip() or DEFAULT_TEMPLATE))
        return AdminResult(ok=True, field=copy.ADMIN_YT_ADD, before="—", after=parsed)
    if control_id == "yt.pick":
        try:
            feed_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        _store(ctx.guild_id, replace(draft, selected_id=feed_id))
        return AdminResult(ok=True)
    if control_id == "yt.channel":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        if draft.yt_channel_id:
            created = await service.add_feed(
                ctx.guild_id,
                yt_channel_id=draft.yt_channel_id,
                discord_channel_id=channel_id,
                actor_id=ctx.actor_id,
                template=draft.template,
            )
            if created == "bad_id":
                return AdminResult(ok=False, error=copy.ADMIN_YT_BAD_ID)
            if created == "duplicate":
                return AdminResult(ok=False, error=copy.ADMIN_YT_DUP)
            _store(ctx.guild_id, _Draft(selected_id=int(created.id)))
            return AdminResult(
                ok=True,
                field=copy.ADMIN_YT_CHANNEL,
                before="—",
                after=_mention_channel(channel_id),
            )
        if draft.selected_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        before = await service.set_channel(ctx.guild_id, int(draft.selected_id), channel_id)
        if before is None and not await _exists(service, ctx.guild_id, int(draft.selected_id)):
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_YT_CHANNEL,
            before=_mention_channel(before),
            after=_mention_channel(channel_id),
        )
    if control_id == "yt.role":
        raw = value.strip()
        if not raw:
            role_id = None
        else:
            try:
                role_id = int(raw.split()[0])
            except (TypeError, ValueError):
                return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
        if draft.selected_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        before = await service.set_role(ctx.guild_id, int(draft.selected_id), role_id)
        if before is None and not await _exists(service, ctx.guild_id, int(draft.selected_id)):
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_YT_ROLE,
            before=_mention_role(before),
            after=_mention_role(role_id),
        )
    if control_id == "yt.remove":
        if draft.selected_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        removed = await service.remove_feed(ctx.guild_id, int(draft.selected_id))
        if not removed:
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        _store(ctx.guild_id, _Draft())
        return AdminResult(ok=True, field=copy.ADMIN_YT_REMOVE, before=str(draft.selected_id), after="—")
    if control_id == "yt.check":
        if draft.selected_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_YT_NEED_FEED)
        posted = await service.poll_feed(int(draft.selected_id))
        after = await _check_after(service, ctx.guild_id, int(draft.selected_id), posted)
        return AdminResult(ok=True, field=copy.ADMIN_YT_CHECK, before="—", after=after)
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


async def _exists(service: object, guild_id: int, feed_id: int) -> bool:
    listing = getattr(service, "feeds", None)
    if not callable(listing):
        return False
    return any(int(item.id) == int(feed_id) for item in await listing(guild_id))


async def _check_after(service: object, guild_id: int, feed_id: int, posted: int) -> str:
    listing = getattr(service, "feeds", None)
    feed = None
    if callable(listing):
        feed = next((item for item in await listing(guild_id) if int(item.id) == int(feed_id)), None)
    if feed is not None and feed.last_error:
        return copy.youtube_check_error(feed.last_error)
    if posted > 0:
        return copy.youtube_check_posted(posted)
    return copy.YOUTUBE_CHECK_IDLE


def _feed_label(item: object) -> str:
    title = getattr(item, "yt_channel_title", "") or getattr(item, "yt_channel_id", "")
    return str(title)[:80]


def _feed_lines(feeds: tuple[object, ...]) -> str:
    if not feeds:
        return "—"
    lines: list[str] = []
    for item in feeds:
        lines.append(
            copy.youtube_feed_line(
                getattr(item, "yt_channel_title", "") or "",
                getattr(item, "yt_channel_id", ""),
                getattr(item, "discord_channel_id", None),
                getattr(item, "last_error", None),
            )
        )
    return "\n".join(lines)


PAGE = AdminPage(
    PAGE_YOUTUBE,
    copy.ADMIN_PAGE_YOUTUBE,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"yt": CONTENT_MANAGE},
)
