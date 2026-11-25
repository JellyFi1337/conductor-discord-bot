"""Страница /admin «Данные»."""

from __future__ import annotations

from conductor.db.session import session_scope
from conductor.domain.access import CONFIG_READ, DATA_FORGET, DATA_MANAGE, STATS_INGEST
from conductor.domain.admin import (
    PAGE_DATA,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    parse_int_group,
)
from conductor.presentation import copy
from conductor.services.guild_config import get_or_create_config

_PICKED: dict[int, int] = {}


def _user(guild_id: int) -> int | None:
    found = _PICKED.get(int(guild_id))
    return None if found is None or found <= 0 else found


def _mention(user_id: int | None) -> str:
    return f"<@{int(user_id)}>" if user_id else "—"


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report, ready_issue

    async with session_scope(ctx.bot.sessions) as session:
        row = await get_or_create_config(session, ctx.guild_id)
        done = row.census_at is not None
    if done:
        return pack_report(())
    return pack_report([ready_issue("empty", fix_control="data.census")])


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    picked = _user(ctx.guild_id)
    busy = int(ctx.guild_id) in set(getattr(ctx.bot, "history_busy", ()))
    async with session_scope(ctx.bot.sessions) as session:
        row = await get_or_create_config(session, ctx.guild_id)
        census = "—" if row.census_at is None else str(row.census_at)
    return PageSnapshot(
        fields={
            copy.ADMIN_DATA_USER: _mention(picked),
            copy.ADMIN_DATA_CENSUS: census,
            copy.ADMIN_DATA_INGEST: copy.INGEST_BUSY_TITLE if busy else copy.ADMIN_DATA_IDLE,
        },
        controls=(
            PageControl("user", "data.user", copy.ADMIN_DATA_USER),
            PageControl("button", "data.export", copy.ADMIN_DATA_EXPORT),
            PageControl("button", "data.forget", copy.ADMIN_DATA_FORGET),
            PageControl("button", "data.census", copy.ADMIN_DATA_CENSUS),
            PageControl("button", "data.ingest", copy.ADMIN_DATA_INGEST),
        ),
        footnote=copy.ADMIN_DATA_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id == "data.user":
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_USER)
        _PICKED[int(ctx.guild_id)] = user_id
        return AdminResult(ok=True, field=copy.ADMIN_DATA_USER, before="—", after=_mention(user_id))
    if control_id == "data.export":
        user_id = _user(ctx.guild_id)
        if user_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_USER)
        payload, name = await ctx.bot.privacy.export_person(ctx.guild_id, user_id)
        return AdminResult(
            ok=True,
            file=payload,
            filename=name,
            field=copy.ADMIN_DATA_EXPORT,
            before="—",
            after=_mention(user_id),
        )
    if control_id == "data.forget":
        user_id = _user(ctx.guild_id)
        if user_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_USER)
        return AdminResult(
            ok=True,
            confirm="data.forget.yes",
            title=copy.DATA_FORGET_CONFIRM,
            after=str(user_id),
        )
    if control_id == "data.forget.yes":
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            user_id = _user(ctx.guild_id)
        if user_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_USER)
        report = await ctx.bot.privacy.forget(ctx.guild_id, user_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_DATA_FORGET,
            before=_mention(user_id),
            after=copy.data_forget_body(rows=report.db_rows, cache=report.cache),
        )
    if control_id == "data.census":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        result = await ctx.bot.run_census(ctx.guild)
        invites = getattr(ctx.bot, "invites", None)
        refresh = getattr(invites, "refresh", None)
        if callable(refresh):
            await refresh(ctx.guild)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_DATA_CENSUS,
            before="—",
            after=str(getattr(result, "humans", "ok")),
        )
    if control_id == "data.ingest":
        return AdminResult(
            ok=True,
            modal="numbers",
            target="data.ingest.set",
            title=copy.ADMIN_DATA_INGEST,
            labels=(copy.ADMIN_DATA_PER_CHANNEL, copy.ADMIN_DATA_REACTIONS),
            defaults=("0", "1"),
        )
    if control_id == "data.ingest.set":
        parsed = parse_int_group(value.replace("\n", " "), count=2)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        per_channel, reactions = parsed
        if per_channel != 0 and (per_channel < 100 or per_channel > 100000):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER)
        if reactions not in {0, 1}:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER)
        busy = set(getattr(ctx.bot, "history_busy", ()))
        if int(ctx.guild_id) in busy:
            return AdminResult(ok=False, error=copy.INGEST_BUSY_BODY)
        return AdminResult(ok=True, progress="ingest", after=f"{per_channel} {reactions}")
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_DATA,
    copy.ADMIN_PAGE_DATA,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={
        "data.user": DATA_MANAGE,
        "data.export": DATA_MANAGE,
        "data.forget": DATA_FORGET,
        "data.census": STATS_INGEST,
        "data.ingest": STATS_INGEST,
    },
)
