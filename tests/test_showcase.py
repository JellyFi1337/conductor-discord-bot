from __future__ import annotations

from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import func, select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import LfgSettings, Panel, StatsBoard
from conductor.db.session import session_scope
from conductor.domain.access import SHOWCASE_POST, AccessSnapshot, Rank, empty_rank_roles
from conductor.domain.showcase import ShowcaseState, showcase_by_id, state_of
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import PANEL_LIVE, PANEL_STALE
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.admin_pages import AdminCtx, page_by_id
from conductor.services.cache import CacheHub
from conductor.services.panels import record_panel
from conductor.services.showcase import ShowcaseService

_STATES = {
    (False, False, None, False): ShowcaseState.OFF,
    (False, False, None, True): ShowcaseState.OFF,
    (False, False, False, False): ShowcaseState.OFF,
    (False, False, False, True): ShowcaseState.OFF,
    (False, False, True, False): ShowcaseState.OFF,
    (False, False, True, True): ShowcaseState.OFF,
    (False, True, None, False): ShowcaseState.OFF,
    (False, True, None, True): ShowcaseState.OFF,
    (False, True, False, False): ShowcaseState.OFF,
    (False, True, False, True): ShowcaseState.OFF,
    (False, True, True, False): ShowcaseState.OFF,
    (False, True, True, True): ShowcaseState.OFF,
    (True, False, None, False): ShowcaseState.MISSING,
    (True, False, None, True): ShowcaseState.MISSING,
    (True, False, False, False): ShowcaseState.MISSING,
    (True, False, False, True): ShowcaseState.MISSING,
    (True, False, True, False): ShowcaseState.MISSING,
    (True, False, True, True): ShowcaseState.MISSING,
    (True, True, False, False): ShowcaseState.STALE,
    (True, True, False, True): ShowcaseState.STALE,
    (True, True, None, False): ShowcaseState.MOVED,
    (True, True, None, True): ShowcaseState.LIVE,
    (True, True, True, False): ShowcaseState.MOVED,
    (True, True, True, True): ShowcaseState.LIVE,
}


@pytest.mark.parametrize(
    ("configured", "row_live", "message_alive", "channel_matches"),
    list(product((False, True), (False, True), (None, False, True), (False, True))),
)
def test_state_of_covers_every_combination(
    configured: bool,
    row_live: bool,
    message_alive: bool | None,
    channel_matches: bool,
) -> None:
    expected = _STATES[(configured, row_live, message_alive, channel_matches)]
    assert (
        state_of(
            configured=configured,
            row_live=row_live,
            message_alive=message_alive,
            channel_matches=channel_matches,
        )
        is expected
    )
    if configured and row_live and message_alive is None and channel_matches:
        assert expected is ShowcaseState.LIVE


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'showcase.db').as_posix()}"


class _AckTrace:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.response = _AckResponse(self.order)


class _AckResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def is_done(self) -> bool:
        return any(item in {"defer", "send_modal", "send_message", "edit_message"} for item in self._order)

    async def defer(self, **_kwargs: object) -> None:
        self._order.append("defer")

    async def send_message(self, **kwargs: object) -> None:
        self._order.append("send_message")
        self.last_message = kwargs


class _Channel:
    def __init__(self, channel_id: int, *, missing: bool = False) -> None:
        self.id = channel_id
        self._missing = missing
        self.deleted: list[int] = []

    async def fetch_message(self, message_id: int) -> SimpleNamespace:
        if self._missing:
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Message"})
        channel = self

        async def delete() -> None:
            channel.deleted.append(int(message_id))

        return SimpleNamespace(id=int(message_id), delete=delete)


def _guild(channels: dict[int, _Channel]) -> SimpleNamespace:
    return SimpleNamespace(id=1, get_channel=lambda channel_id: channels.get(int(channel_id)))


async def _bot(tmp_path: Path, channels: dict[int, _Channel] | None = None):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    found = {} if channels is None else channels
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        lfg=SimpleNamespace(post_starter=AsyncMock(return_value=True), set_channel=AsyncMock(return_value=None)),
        stats_board=SimpleNamespace(post=AsyncMock(return_value=True), set_channel=AsyncMock(return_value=None)),
        verification=SimpleNamespace(post_cards=AsyncMock(return_value=True), bind=AsyncMock()),
        self_roles=SimpleNamespace(
            ensure_defaults=AsyncMock(),
            menus=AsyncMock(return_value=()),
            post_menu=AsyncMock(return_value=None),
        ),
        tickets=SimpleNamespace(post_panel=AsyncMock(return_value=True)),
        automod=SimpleNamespace(post_card=AsyncMock(return_value=True)),
        refresh_config=AsyncMock(),
    )
    bot.showcase = ShowcaseService(bot)
    guild = _guild(found)
    return bot, engine, guild


async def _live_count(sessions: object, *, kind: str) -> int:
    async with session_scope(sessions) as session:  # type: ignore[arg-type]
        value = await session.scalar(
            select(func.count()).select_from(Panel).where(Panel.kind == kind, Panel.state == PANEL_LIVE)
        )
    return int(value or 0)


async def test_status_not_found_marks_stale(tmp_path: Path) -> None:
    channel = _Channel(20, missing=True)
    bot, engine, guild = await _bot(tmp_path, {20: channel})
    spec = showcase_by_id("lfg")
    assert spec is not None
    try:
        async with session_scope(bot.sessions) as session:
            session.add(LfgSettings(guild_id=1, channel_id=20))
            await record_panel(
                session,
                kind="lf",
                guild_id=1,
                channel_id=20,
                message_id=30,
                ref="1",
                created_by=7,
            )
        state = await bot.showcase.status(guild, spec)
        assert state is ShowcaseState.STALE
        async with session_scope(bot.sessions) as session:
            row = await session.scalar(select(Panel).where(Panel.kind == "lf"))
            assert row is not None
            assert row.state == PANEL_STALE
    finally:
        await engine.dispose()


async def test_post_twice_leaves_one_live_row(tmp_path: Path) -> None:
    channel = _Channel(20)
    bot, engine, guild = await _bot(tmp_path, {20: channel})
    spec = showcase_by_id("lfg")
    assert spec is not None
    next_id = {"n": 100}

    async def post_starter(_guild: object, posted: object, *, actor_id: int) -> bool:
        next_id["n"] += 1
        async with session_scope(bot.sessions) as session:
            await record_panel(
                session,
                kind="lf",
                guild_id=1,
                channel_id=int(posted.id),
                message_id=next_id["n"],
                ref="1",
                created_by=actor_id,
            )
        return True

    bot.lfg.post_starter = post_starter
    try:
        async with session_scope(bot.sessions) as session:
            session.add(LfgSettings(guild_id=1, channel_id=20))
        first = await bot.showcase.post(guild, spec, channel_id=20, actor_id=7)
        second = await bot.showcase.post(guild, spec, channel_id=20, actor_id=7)
        assert first.ok and second.ok
        assert await _live_count(bot.sessions, kind="lf") == 1
    finally:
        await engine.dispose()


async def test_post_to_other_channel_is_moved_on_status(tmp_path: Path) -> None:
    bound = _Channel(20)
    other = _Channel(30)
    bot, engine, guild = await _bot(tmp_path, {20: bound, 30: other})
    spec = showcase_by_id("lfg")
    assert spec is not None
    next_id = {"n": 200}

    async def post_starter(_guild: object, posted: object, *, actor_id: int) -> bool:
        next_id["n"] += 1
        async with session_scope(bot.sessions) as session:
            await record_panel(
                session,
                kind="lf",
                guild_id=1,
                channel_id=int(posted.id),
                message_id=next_id["n"],
                ref="1",
                created_by=actor_id,
            )
        return True

    bot.lfg.post_starter = post_starter
    try:
        async with session_scope(bot.sessions) as session:
            session.add(LfgSettings(guild_id=1, channel_id=20))
        result = await bot.showcase.post(guild, spec, channel_id=30, actor_id=7)
        assert result.ok
        assert bot.lfg.set_channel.await_count == 0
        assert await bot.showcase.status(guild, spec) is ShowcaseState.MOVED
        async with session_scope(bot.sessions) as session:
            settings = await session.get(LfgSettings, 1)
            assert settings is not None
            assert int(settings.channel_id or 0) == 20
    finally:
        await engine.dispose()


async def test_post_missing_skips_live(tmp_path: Path) -> None:
    lfg_channel = _Channel(20)
    stats_channel = _Channel(40)
    bot, engine, guild = await _bot(tmp_path, {20: lfg_channel, 40: stats_channel})
    live_calls: list[str] = []

    async def post_lfg(_guild: object, posted: object, *, actor_id: int) -> bool:
        live_calls.append("lfg")
        async with session_scope(bot.sessions) as session:
            await record_panel(
                session,
                kind="lf",
                guild_id=1,
                channel_id=int(posted.id),
                message_id=501,
                ref="1",
                created_by=actor_id,
            )
        return True

    async def post_stats(_guild: object, posted: object, *, actor_id: int) -> bool:
        live_calls.append("stats")
        async with session_scope(bot.sessions) as session:
            await record_panel(
                session,
                kind="st",
                guild_id=1,
                channel_id=int(posted.id),
                message_id=502,
                ref="1",
                created_by=actor_id,
            )
        return True

    bot.lfg.post_starter = post_lfg
    bot.stats_board.post = post_stats
    try:
        async with session_scope(bot.sessions) as session:
            session.add(LfgSettings(guild_id=1, channel_id=20))
            session.add(StatsBoard(guild_id=1, channel_id=40))
            await record_panel(
                session,
                kind="lf",
                guild_id=1,
                channel_id=20,
                message_id=30,
                ref="1",
                created_by=7,
            )
        posted = await bot.showcase.post_missing(guild, actor_id=7)
        assert "lfg" not in live_calls
        assert "stats" in live_calls
        assert any(item.ok for item in posted)
        assert await bot.showcase.status(guild, showcase_by_id("lfg")) is ShowcaseState.LIVE  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def test_apply_control_denies_post_without_key(tmp_path: Path) -> None:
    from conductor.services.admin_panel import apply_control

    bot, engine, _guild = await _bot(tmp_path)
    ranks = empty_rank_roles()
    ranks[Rank.SENIOR] = (7,)
    snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), snapshot)
    interaction = _AckTrace()
    page = page_by_id("showcase")
    assert page is not None
    assert page.keys["show"] == SHOWCASE_POST
    try:
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2, member_role_ids=(7,))
        await apply_control(interaction, ctx, page, "show.post:lfg", "")  # type: ignore[arg-type]
        bot.lfg.post_starter.assert_not_awaited()
        assert "defer" not in interaction.order
        assert "send_message" in interaction.order
        text = collected_text(interaction.response.last_message["view"])  # type: ignore[attr-defined]
        assert copy.DENIED_TITLE in text
        assert copy.denied_actors(Rank.CHIEF.key) in text
    finally:
        await engine.dispose()
