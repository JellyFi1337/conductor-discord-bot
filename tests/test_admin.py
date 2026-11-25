from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import (
    PollSettings,
    PulseAlertSettings,
    StatsBoard,
    TicketCategory,
    TicketPanel,
    VerificationSettings,
    VoiceHub,
    YoutubeFeed,
)
from conductor.db.session import session_scope
from conductor.domain.access import (
    ACTION_KEYS,
    ACTIVITY_CONFIG,
    CONFIG_MANAGE,
    CONFIG_READ,
    DATA_FORGET,
    DATA_MANAGE,
    GIVEAWAY_MANAGE,
    KEY_SPECS,
    POLL_MANAGE,
    PULSE_ALERTS,
    SHOWCASE_POST,
    SKELETON_READ,
    STATS_INGEST,
    TICKETS_FORM_MANAGE,
    TICKETS_PANEL_POST,
    VOICE_HUB_MANAGE,
    VOICE_PRESET,
    AccessSnapshot,
    Rank,
    empty_rank_roles,
)
from conductor.domain.admin import (
    COMMON_TIMEZONES,
    PICK_EMPTY_VALUE,
    ROLE_CREATE_VALUE,
    PageControl,
    ReadyIssue,
    ReadyReport,
    ReadyState,
    control_key,
    parse_duration,
    parse_id_list,
    parse_int_group,
    parse_int_range,
    parse_timezone,
    perm_chunks,
    usable_timezones,
)
from conductor.domain.quotas import QuotaTable
from conductor.domain.rooms import HubPreset, parse_hub_settings
from conductor.presentation import copy
from conductor.presentation.admin import (
    DISCORD_SELECT_LIMIT,
    HOME_SELECT_CHUNK,
    home_card,
    issues_card,
    no_pages_card,
    number_out_of_range,
    page_card,
)
from conductor.presentation.layout import COMPONENT_LIMIT, TEXT_LIMIT, collected_text
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.achievements import AchievementService
from conductor.services.admin_data import _PICKED
from conductor.services.admin_pages import (
    AdminCtx,
    AdminPage,
    all_pages,
    filter_controls,
    free_roles,
    may_use,
    member_access_issue,
    page_by_id,
    pages_for,
    register,
)
from conductor.services.admin_self_roles import _DRAFTS as _SR_DRAFTS
from conductor.services.admin_tickets import _DRAFTS
from conductor.services.admin_youtube import _DRAFTS as _YT_DRAFTS
from conductor.services.cache import CacheHub
from conductor.services.giveaway import GiveawayService
from conductor.services.guild_config import forget_tz, remember_tz, tz_for
from conductor.services.ladder import LadderService
from conductor.services.poll import PollService
from conductor.services.privacy import PrivacyService
from conductor.services.pulse import PulseService
from conductor.services.recruit import RecruitService
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleSubsystem
from conductor.services.self_roles import SelfRolesService
from conductor.services.stats_board import StatsBoardService
from conductor.services.tickets import TicketService
from conductor.services.verification import VerificationService
from conductor.services.voice_rooms import VoiceRoomService
from conductor.services.youtube import YoutubeService

ADMIN_PY = Path(__file__).resolve().parents[1] / "src" / "conductor" / "domain" / "admin.py"


def test_page_ids_are_unique() -> None:
    ids = [page.id for page in all_pages()]
    assert ids == [
        "skeleton",
        "general",
        "logs",
        "ranks",
        "perms",
        "staffing",
        "mute",
        "quotas",
        "old",
        "reputation",
        "ladder",
        "achievements",
        "giveaway",
        "voice",
        "tickets",
        "automod",
        "verification",
        "selfroles",
        "showcase",
        "stats",
        "polls",
        "youtube",
        "lfg",
        "groups",
        "recruit",
        "alerts",
        "data",
    ]
    assert {page.key for page in all_pages()} == {
        CONFIG_READ,
        ACTIVITY_CONFIG,
        GIVEAWAY_MANAGE,
        VOICE_HUB_MANAGE,
        TICKETS_PANEL_POST,
        PULSE_ALERTS,
        POLL_MANAGE,
        SKELETON_READ,
    }
    voice = page_by_id("voice")
    assert voice is not None
    assert voice.keys["voice.hub"] == VOICE_HUB_MANAGE
    assert voice.keys["voice.preset"] == VOICE_PRESET
    tickets = page_by_id("tickets")
    assert tickets is not None
    assert tickets.keys["tickets.post"] == TICKETS_PANEL_POST
    assert tickets.keys["tickets.add"] == TICKETS_FORM_MANAGE
    showcase = page_by_id("showcase")
    assert showcase is not None
    assert showcase.keys["show"] == SHOWCASE_POST
    data = page_by_id("data")
    assert data is not None
    assert data.keys["data.export"] == DATA_MANAGE
    assert data.keys["data.forget"] == DATA_FORGET
    assert data.keys["data.census"] == STATS_INGEST


def test_control_key_maps_prefix_and_falls_back_to_page() -> None:
    data = page_by_id("data")
    tickets = page_by_id("tickets")
    voice = page_by_id("voice")
    general = page_by_id("general")
    assert data is not None and tickets is not None and voice is not None and general is not None
    assert control_key(data, "data.forget") == DATA_FORGET
    assert control_key(data, "data.forget.yes") == DATA_FORGET
    assert control_key(data, "data.ingest.set") == STATS_INGEST
    assert control_key(data, "data.export") == DATA_MANAGE
    assert control_key(tickets, "tickets.kind:Вопрос") == TICKETS_FORM_MANAGE
    assert control_key(tickets, "tickets.form.saved") == TICKETS_FORM_MANAGE
    assert control_key(tickets, "tickets.post") == TICKETS_PANEL_POST
    assert control_key(voice, "voice.template.set") == VOICE_PRESET
    assert control_key(voice, "voice.hub") == VOICE_HUB_MANAGE
    assert control_key(general, "general.tz") == CONFIG_MANAGE


def test_register_rejects_duplicate_id() -> None:
    page = all_pages()[0]
    with pytest.raises(ValueError, match="duplicate"):
        register(page)


def test_pages_for_filters_by_keys() -> None:
    locked = AccessSnapshot(rank_roles=empty_rank_roles(), rules={}, special_locked=True)
    assert pages_for(locked, member_role_ids=()) == ()
    ranks = empty_rank_roles()
    ranks[Rank.SPECIAL] = (99,)
    special = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    allowed = pages_for(special, member_role_ids=(99,))
    assert tuple(page.id for page in allowed) == tuple(page.id for page in all_pages())
    chief_ranks = empty_rank_roles()
    chief_ranks[Rank.CHIEF] = (7,)
    chief = AccessSnapshot(rank_roles=chief_ranks, rules={}, special_locked=True)
    chief_pages = pages_for(chief, member_role_ids=(7,))
    assert len(chief_pages) >= 18
    senior_ranks = empty_rank_roles()
    senior_ranks[Rank.SENIOR] = (3,)
    senior = AccessSnapshot(rank_roles=senior_ranks, rules={}, special_locked=True)
    senior_ids = {page.id for page in pages_for(senior, member_role_ids=(3,))}
    assert "giveaway" in senior_ids
    assert "polls" in senior_ids
    assert "general" in senior_ids
    watcher_ranks = empty_rank_roles()
    watcher_ranks[Rank.WATCHER] = (1,)
    watcher = AccessSnapshot(rank_roles=watcher_ranks, rules={}, special_locked=True)
    assert pages_for(watcher, member_role_ids=(1,)) == ()


def test_perms_paginates_ten_keys() -> None:
    chunks = perm_chunks(ACTION_KEYS)
    assert chunks
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == len(ACTION_KEYS)
    page = page_by_id("perms")
    assert page is not None


def test_number_out_of_range_is_denied_card() -> None:
    assert parse_int_range("3", lo=1, hi=10) == 3
    assert parse_int_range("99", lo=1, hi=10) is None
    card = number_out_of_range("99", lo=1, hi=10)
    assert not isinstance(card, int)
    text = collected_text(card)
    assert copy.DENIED_TITLE in text
    assert copy.ADMIN_BAD_NUMBER in text
    assert number_out_of_range("4", lo=1, hi=10) == 4


def test_no_page_keys_is_denied_card() -> None:
    text = collected_text(no_pages_card())
    assert copy.DENIED_TITLE in text
    assert copy.ADMIN_NO_PAGES in text


def test_home_card_splits_selects_before_discord_limit() -> None:
    async def on_page(_interaction: object, _page_id: str) -> None:
        return None

    assert HOME_SELECT_CHUNK < DISCORD_SELECT_LIMIT
    pages = [(f"p{index}", f"Страница {index}") for index in range(25)]
    view = home_card(fields={}, pages=pages, on_page=on_page)
    selects = [item for item in view.walk_children() if isinstance(item, discord.ui.Select)]
    assert len(selects) == 3
    assert all(len(item.options) <= HOME_SELECT_CHUNK for item in selects)
    assert [option.value for item in selects for option in item.options] == [f"p{index}" for index in range(25)]
    compact = home_card(fields={}, pages=pages[:HOME_SELECT_CHUNK], on_page=on_page)
    one = [item for item in compact.walk_children() if isinstance(item, discord.ui.Select)]
    assert len(one) == 1
    assert one[0].placeholder == copy.ADMIN_PAGES

    async def on_issues(_interaction: object) -> None:
        return None

    with_issues = home_card(fields={}, pages=pages[:1], on_page=on_page, on_issues=on_issues)
    labels = [getattr(item, "label", None) for item in with_issues.walk_children()]
    assert copy.ADMIN_SHOW_ISSUES in labels


def test_timezone_and_lists_parse() -> None:
    assert parse_timezone("UTC") == "UTC"
    assert parse_timezone("not-a-zone") is None
    assert parse_timezone("") is None
    assert len(COMMON_TIMEZONES) == 25
    assert "UTC" in usable_timezones()
    assert parse_id_list("1 2,3") == (1, 2, 3)
    assert parse_id_list("1 x") is None
    assert parse_int_group("5 2 2 20 10 25 40", count=7) == (5, 2, 2, 20, 10, 25, 40)
    assert parse_int_group("1 2", count=3) is None
    assert parse_duration("30м") is not None
    assert parse_duration("нет") is None


def test_tz_cache_remembers_zone() -> None:
    forget_tz()
    assert getattr(tz_for(1), "key", "UTC") == "UTC"
    remember_tz(1, "UTC")
    assert getattr(tz_for(1), "key", "UTC") == "UTC"
    remember_tz(1, "not-a-zone")
    assert getattr(tz_for(1), "key", "UTC") == "UTC"
    forget_tz(1)
    assert getattr(tz_for(1), "key", "UTC") == "UTC"
    forget_tz()


def test_domain_admin_has_no_discord_or_sqlalchemy() -> None:
    source = ADMIN_PY.read_text(encoding="utf-8")
    assert "discord" not in source
    assert "sqlalchemy" not in source


def test_admin_page_type_is_registry_row() -> None:
    page = all_pages()[0]
    assert isinstance(page, AdminPage)
    assert page.id == "skeleton"
    assert page.label == copy.ADMIN_PAGE_SKELETON


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _bot(tmp_path: Path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)

    async def refresh(_guild_id: int) -> None:
        return None

    async def run_census(_guild: object) -> SimpleNamespace:
        return SimpleNamespace(humans=3, bots=0, boosters=0, created=0, updated=0)

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        roles=SimpleNamespace(bind=lambda *_args, **_kwargs: None, owner_of=lambda *_a, **_k: None),
        refresh_config=refresh,
        watchers=SimpleNamespace(notify_special_locked=None),
        spawn=lambda coro, *_args, **_kwargs: coro.close() if hasattr(coro, "close") else None,
        moderation=SimpleNamespace(
            set_mute_role=refresh,
            quota_table=AsyncMock(return_value=QuotaTable(limits={})),
        ),
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: None),
        config_by_guild={},
        loyalty=SimpleNamespace(protect_guild=None),
        history_busy=set(),
        run_census=run_census,
        invites=SimpleNamespace(refresh=refresh),
        queue=SimpleNamespace(
            schedule_later=lambda *_args, **_kwargs: None,
            cancel_later=lambda *_args, **_kwargs: None,
            until=lambda *_args, **_kwargs: 0,
        ),
    )
    bot.reputation = ReputationService(bot)
    bot.achievements = AchievementService(bot)
    bot.ladder = LadderService(bot)
    bot.giveaways = GiveawayService(bot)
    bot.rooms = VoiceRoomService(bot)
    bot.tickets = TicketService(bot)
    bot.verification = VerificationService(bot)
    bot.self_roles = SelfRolesService(bot)
    bot.stats_board = StatsBoardService(bot)
    bot.polls = PollService(bot)
    bot.youtube = YoutubeService(bot)
    bot.pulse = PulseService(bot)
    bot.recruit = RecruitService(bot)
    bot.privacy = PrivacyService(bot)
    return bot, engine


_LIVE_CATEGORY_ID = 90_001


def _open_overwrite() -> SimpleNamespace:
    return SimpleNamespace(
        view_channel=None,
        send_messages=None,
        connect=None,
        speak=None,
        read_message_history=None,
        manage_channels=None,
        move_members=None,
    )


def _live_category(category_id: int = _LIVE_CATEGORY_ID) -> SimpleNamespace:
    perms = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        connect=True,
        speak=True,
        read_message_history=True,
        manage_channels=True,
        move_members=True,
    )
    return SimpleNamespace(
        id=int(category_id),
        type="category",
        category=None,
        category_id=None,
        channels=[],
        overwrites={},
        overwrites_for=lambda _target: _open_overwrite(),
        permissions_for=lambda _member: perms,
    )


def _live_channel(channel_id: int) -> SimpleNamespace:
    perms = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        connect=True,
        speak=True,
        read_message_history=True,
        manage_channels=True,
        move_members=True,
    )
    category = _live_category()
    return SimpleNamespace(
        id=int(channel_id),
        category=category,
        category_id=_LIVE_CATEGORY_ID,
        overwrites={},
        overwrites_for=lambda _target: _open_overwrite(),
        permissions_for=lambda _member: perms,
    )


def _live_role(role_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=int(role_id),
        permissions=SimpleNamespace(view_channel=True, send_messages=True, connect=True, speak=True),
    )


def _ctx(bot: object, *, guild_id: int = 1, actor_id: int = 2) -> AdminCtx:
    everyone = _live_role(guild_id)
    category = _live_category()

    def get_channel(channel_id: int) -> SimpleNamespace:
        cid = int(channel_id)
        if cid == _LIVE_CATEGORY_ID:
            return category
        return _live_channel(cid)

    return AdminCtx(
        bot=bot,
        guild_id=guild_id,
        actor_id=actor_id,
        guild=SimpleNamespace(
            id=guild_id,
            default_role=everyone,
            me=SimpleNamespace(id=99),
            get_channel=get_channel,
            get_role=lambda role_id: everyone if int(role_id) == guild_id else _live_role(int(role_id)),
        ),
    )


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


async def _render(page: AdminPage, ctx: AdminCtx):
    snap = await page.snapshot(ctx, 1)
    return page_card(
        title=page.label,
        fields=snap.fields,
        footnote=snap.footnote,
        controls=snap.controls,
        on_control=_noop,
        on_home=_noop,
    )


async def test_perms_page_turns(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
        page = page_by_id("perms")
        assert page is not None
        first = await page.snapshot(ctx, 1)
        turned = await page.handle(ctx, "perms.next:2", "")
        assert turned.ok is True
        assert turned.leaf == 2
        second = await page.snapshot(ctx, 2)
        assert first.fields
        assert second.fields
        assert tuple(first.fields) != tuple(second.fields)
    finally:
        await engine.dispose()


async def test_log_channel_rejects_hub(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        async with session_scope(bot.sessions) as session:
            session.add(VoiceHub(guild_id=1, channel_id=55))
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
        page = page_by_id("logs")
        assert page is not None
        result = await page.handle(ctx, "logs.user", "55")
        assert result.ok is False
        assert result.error == copy.ADMIN_LOG_IS_HUB
    finally:
        await engine.dispose()


async def test_logs_page_sets_user_log_keep_days(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
        page = page_by_id("logs")
        assert page is not None
        opened = await page.handle(ctx, "logs.keep", "")
        assert opened.modal == "number"
        assert opened.target == "logs.keep.set"
        assert opened.lo == 0
        saved = await page.handle(ctx, "logs.keep.set", "30")
        assert saved.ok is True
        assert saved.after == "30"
        snap = await page.snapshot(ctx, 1)
        assert snap.fields[copy.ADMIN_FIELD_USER_LOG_KEEP] == "30"
    finally:
        await engine.dispose()


def _assert_v2(view) -> None:
    assert len(collected_text(view)) <= TEXT_LIMIT
    assert view.total_children_count <= COMPONENT_LIMIT


async def test_reputation_page_writes_config(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("reputation")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        before = await bot.reputation.show_config(1)
        opened = await page.handle(ctx, "rep.growth", "")
        assert opened.modal == "numbers"
        written = await page.handle(ctx, "rep.growth.set", "80 15")
        assert written.ok is True
        hits = await page.handle(ctx, "rep.hits.set", "6 3 3 15 11 26 30")
        assert hits.ok is True
        after = await bot.reputation.show_config(1)
        assert before.config.messages_per_point != 80
        assert after.config.messages_per_point == 80
        assert after.config.voice_seconds_per_point == 15 * 60
        assert after.config.warn_hit == 6
        assert after.config.max_hit_per_hour == 30
        bound = await page.handle(ctx, "rep.tier:0", "77")
        assert bound.ok is True
        panel = await bot.reputation.show_config(1)
        assert panel.tiers[0].role_id == 77
        for index in range(1, 6):
            await page.handle(ctx, f"rep.tier:{index}", str(80 + index))
        assert (await page.ready(ctx)).ok is True
    finally:
        await engine.dispose()


async def test_ladder_page_writes_config(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("ladder")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        before = await bot.ladder.show_config(1)
        written = await page.handle(ctx, "ladder.friend_group.set", "60 2000 80 400")
        assert written.ok is True
        after = await bot.ladder.show_config(1)
        assert before.config.friend_tenure_days != 60
        assert after.config.friend_tenure_days == 60
        assert after.config.friend_messages == 2000
        assert after.config.friend_voice_seconds == 80 * 3600
        assert after.config.friend_reactions == 400
        resident = await page.handle(ctx, "ladder.resident_group.set", "500 50 30 0 0")
        assert resident.ok is True
        assert (await bot.ladder.show_config(1)).config.resident_days == 30
        bound = await page.handle(ctx, "ladder.guest", "21")
        assert bound.ok is True
        assert (await bot.ladder.show_config(1)).roles.guest == 21
    finally:
        await engine.dispose()


async def test_achievements_page_binds_role_and_sets_count(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("achievements")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        snap = await page.snapshot(ctx, 1)
        role_control = next(item.id for item in snap.controls if item.kind == "role_pick")
        bound = await page.handle(ctx, role_control, "201")
        assert bound.ok is True, bound.error
        panel = await bot.achievements.show_config(1)
        fixer = next(item for item in panel.defs if item.key == "fixer" and item.stage == 1)
        assert fixer.role_id == 201
        picked = await page.handle(ctx, "achieve.user:1", "7")
        assert picked.ok is True
        opened = await page.handle(ctx, "achieve.count:1", "")
        assert opened.modal == "achieve_count"
        written = await page.handle(ctx, "achieve.count.set:1", "5\nтест")
        assert written.ok is True
        card = await bot.achievements.profile(1, 7)
        line = next(item for item in card.lines if item.key == "fixer")
        assert line.count == 5
        assert line.stage == 2
    finally:
        await engine.dispose()


async def test_giveaway_page_writes_weights_and_roles(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("giveaway")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        before = await bot.giveaways.weight_formula(1)
        written = await page.handle(ctx, "give.weights.set", "10 20 5")
        assert written.ok is True
        after = await bot.giveaways.weight_formula(1)
        assert before != (10, 20, 5)
        assert after == (10, 20, 5)
        added = await page.handle(ctx, "give.roles", "42")
        assert added.ok is True
        assert 42 in await bot.giveaways.whitelist(1)
        removed = await page.handle(ctx, "give.roles", "42")
        assert removed.ok is True
        assert 42 not in await bot.giveaways.whitelist(1)
    finally:
        await engine.dispose()


async def test_voice_page_writes_hub_and_preset(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("voice")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        assert (await page.ready(ctx)).ok is False
        clone = await page.handle(ctx, "voice.preset", HubPreset.CLONE)
        assert clone.ok is False
        assert clone.error == copy.VOICE_NO_HUB
        hub = await page.handle(ctx, "voice.hub", "100")
        assert hub.ok is True
        assert (await page.ready(ctx)).ok is True
        async with session_scope(bot.sessions) as session:
            row = await bot.rooms.hub_for(session, 1)
            assert row is not None
            assert row.channel_id == 100
        missing = await page.handle(ctx, "voice.preset", HubPreset.CLONE)
        assert missing.ok is False
        assert missing.error == copy.ADMIN_VOICE_NEED_TEMPLATE
        named = await page.handle(ctx, "voice.preset", HubPreset.NAME)
        assert named.ok is True
        cloned = await page.handle(ctx, "voice.clone", "200")
        assert cloned.ok is True
        async with session_scope(bot.sessions) as session:
            row = await bot.rooms.hub_for(session, 1)
            assert row is not None
            settings = parse_hub_settings(row.settings)
            assert settings.preset == HubPreset.CLONE
            assert settings.template_channel_id == 200
        _assert_v2(await _render(page, ctx))
    finally:
        await engine.dispose()


async def test_tickets_page_writes_form_help(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    _DRAFTS.clear()
    try:
        async with session_scope(bot.sessions) as session:
            panel = TicketPanel(
                guild_id=1,
                channel_id=3,
                message_id=99,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                created_by=2,
            )
            session.add(panel)
            await session.flush()
            session.add(TicketCategory(panel_id=panel.id, guild_id=1, name="Вопрос", position=0))
        ctx = _ctx(bot)
        page = page_by_id("tickets")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        before = await bot.tickets.category_help(1, "Вопрос")
        assert before is not None
        assert before[0] == ""
        written = await page.handle(ctx, "tickets.hint:Вопрос", "как описать")
        assert written.ok is True
        after = await bot.tickets.category_help(1, "Вопрос")
        assert after is not None
        assert after[0] == "как описать"
        cleared = await page.handle(ctx, "tickets.c:Вопрос", "")
        assert cleared.ok is True
        reset = await bot.tickets.category_help(1, "Вопрос")
        assert reset is not None
        assert reset[0] == ""
        assert reset[1].fields == ()
        assert (await page.ready(ctx)).ok is True
    finally:
        _DRAFTS.clear()
        await engine.dispose()


async def test_verification_page_writes_channel_and_role(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("verification")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        assert (await page.ready(ctx)).ok is False
        channel = await page.handle(ctx, "vf.channel", "55")
        assert channel.ok is True
        role = await page.handle(ctx, "vf.role", "88")
        assert role.ok is True
        async with session_scope(bot.sessions) as session:
            row = await session.get(VerificationSettings, 1)
        assert row is not None
        assert row.channel_id == 55
        assert row.verified_role_id == 88
        assert bot.verification.channel_id(1) == 55
        posted = await page.handle(ctx, "vf.post", "")
        assert posted.ok is False
        assert posted.error == copy.STALE_BODY
    finally:
        await engine.dispose()


async def test_alerts_page_writes_floors(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("alerts")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        opened = await page.handle(ctx, "alerts.floors", "")
        assert opened.modal == "numbers"
        written = await page.handle(ctx, "alerts.floors.set", "10 8 40 20")
        assert written.ok is True
        async with session_scope(bot.sessions) as session:
            row = await session.get(PulseAlertSettings, 1)
        assert row is not None
        assert row.voice_floor == 10
        assert row.leave_floor == 8
        assert row.authors_pct == 40
        assert row.join_spike_floor == 20
        toggled = await page.handle(ctx, "alerts.toggle", "")
        assert toggled.ok is True
        rules = await bot.pulse.alert_rules(1)
        assert rules.enabled is False
    finally:
        await engine.dispose()


async def test_data_forget_needs_confirm(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    _PICKED.clear()
    forget = AsyncMock(return_value=SimpleNamespace(db_rows=2, cache=1, tables={"members": 2}))
    bot.privacy.forget = forget
    try:
        ctx = _ctx(bot)
        page = page_by_id("data")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        picked = await page.handle(ctx, "data.user", "9")
        assert picked.ok is True
        asked = await page.handle(ctx, "data.forget", "")
        assert asked.ok is True
        assert asked.confirm == "data.forget.yes"
        assert asked.after == "9"
        forget.assert_not_awaited()
        done = await page.handle(ctx, "data.forget.yes", "9")
        assert done.ok is True
        forget.assert_awaited_once()
        assert forget.await_args.args == (1, 9)
    finally:
        _PICKED.clear()
        await engine.dispose()


async def test_data_page_hides_forget_from_chief(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("data")
        assert page is not None
        snap = await page.snapshot(ctx, 1)
        ranks = empty_rank_roles()
        ranks[Rank.CHIEF] = (7,)
        snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
        shown = filter_controls(snapshot, page, snap.controls, member_role_ids=(7,))
        assert {item.id for item in shown.allowed} == {"data.census", "data.ingest"}
        special_ranks = empty_rank_roles()
        special_ranks[Rank.SPECIAL] = (9,)
        special = AccessSnapshot(rank_roles=special_ranks, rules={}, special_locked=True)
        full = filter_controls(special, page, snap.controls, member_role_ids=(9,))
        assert {item.id for item in full.allowed} == {item.id for item in snap.controls}
    finally:
        await engine.dispose()


class _AckTrace:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.response = _AckResponse(self.order)

    async def edit_original_response(self, **_kwargs: object) -> None:
        self.order.append("edit_original_response")


class _AckResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def is_done(self) -> bool:
        return any(item in {"defer", "send_modal", "send_message", "edit_message"} for item in self._order)

    async def defer(self, **_kwargs: object) -> None:
        self._order.append("defer")

    async def send_modal(self, _modal: object = None, **_kwargs: object) -> None:
        self._order.append("send_modal")

    async def send_message(self, **kwargs: object) -> None:
        self._order.append("send_message")
        self.last_message = kwargs

    async def edit_message(self, **_kwargs: object) -> None:
        self._order.append("edit_message")


def _chief_data(bot: object) -> None:
    ranks = empty_rank_roles()
    ranks[Rank.CHIEF] = (7,)
    snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), snapshot)  # type: ignore[attr-defined]


async def test_apply_control_denies_forget_without_key(tmp_path: Path) -> None:
    from conductor.services.admin_panel import apply_control

    bot, engine = await _bot(tmp_path)
    forget = AsyncMock(return_value=SimpleNamespace(db_rows=1, cache=0, tables={}))
    bot.privacy.forget = forget
    _chief_data(bot)
    interaction = _AckTrace()
    try:
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2, member_role_ids=(7,))
        page = page_by_id("data")
        assert page is not None
        await apply_control(interaction, ctx, page, "data.forget", "")  # type: ignore[arg-type]
        forget.assert_not_awaited()
        assert "defer" not in interaction.order
        assert "send_message" in interaction.order
        text = collected_text(interaction.response.last_message["view"])  # type: ignore[attr-defined]
        assert copy.DENIED_TITLE in text
        assert copy.denied_actors(Rank.SPECIAL.key) in text
    finally:
        await engine.dispose()


async def test_apply_control_census_defers_before_work(tmp_path: Path) -> None:
    from conductor.services.admin_panel import apply_control

    bot, engine = await _bot(tmp_path)
    _chief_data(bot)
    interaction = _AckTrace()
    inner = bot.run_census

    async def run_census(guild: object) -> SimpleNamespace:
        interaction.order.append("work")
        return await inner(guild)

    bot.run_census = run_census
    try:
        ctx = AdminCtx(
            bot=bot,
            guild_id=1,
            actor_id=2,
            guild=SimpleNamespace(id=1, get_channel=lambda _id: None),
            member_role_ids=(7,),
        )
        page = page_by_id("data")
        assert page is not None
        await apply_control(interaction, ctx, page, "data.census", "")  # type: ignore[arg-type]
        assert interaction.order[0] == "defer"
        assert "work" in interaction.order
        assert interaction.order[-1] == "edit_original_response"
        assert interaction.order.index("defer") < interaction.order.index("work")
        assert interaction.order.index("work") < interaction.order.index("edit_original_response")
    finally:
        await engine.dispose()


async def test_apply_control_ingest_opens_modal_without_defer(tmp_path: Path) -> None:
    from conductor.services.admin_panel import apply_control

    bot, engine = await _bot(tmp_path)
    _chief_data(bot)
    interaction = _AckTrace()
    try:
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2, member_role_ids=(7,))
        page = page_by_id("data")
        assert page is not None
        await apply_control(interaction, ctx, page, "data.ingest", "")  # type: ignore[arg-type]
        assert interaction.order == ["send_modal"]
    finally:
        await engine.dispose()


async def test_data_ingest_busy_is_denied(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    bot.history_busy = {1}
    try:
        ctx = _ctx(bot)
        page = page_by_id("data")
        assert page is not None
        denied = await page.handle(ctx, "data.ingest.set", "0 1")
        assert denied.ok is False
        assert denied.error == copy.INGEST_BUSY_BODY
        assert denied.progress is None
    finally:
        await engine.dispose()


async def test_selfroles_page_adds_role_and_rejects_special(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    _SR_DRAFTS.clear()
    try:
        guild = SimpleNamespace(
            id=1,
            default_role=SimpleNamespace(id=1),
            get_role=lambda role_id: SimpleNamespace(id=int(role_id), managed=False),
            get_channel=lambda _channel_id: None,
        )
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2, guild=guild)
        page = page_by_id("selfroles")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        assert (await page.ready(ctx)).ok is False
        posted = await page.handle(ctx, "sr.post", "55")
        assert posted.ok is False
        assert posted.error == copy.SELFROLES_EMPTY
        added = await page.handle(ctx, "sr.add", "10")
        assert added.modal == "self_role"
        written = await page.handle(ctx, "sr.add.set", "Forts\nигра\n\nnone")
        assert written.ok is True
        options = await bot.self_roles.options(_SR_DRAFTS[1].menu_id or 0)
        assert [item.label for item in options] == ["Forts"]
        ranks = empty_rank_roles()
        ranks[Rank.SPECIAL] = (8,)
        snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
        bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), snapshot)
        special = await page.handle(ctx, "sr.add", "8")
        assert special.ok is False
        assert special.error == copy.SELFROLES_SPECIAL
    finally:
        _SR_DRAFTS.clear()
        await engine.dispose()


async def test_stats_page_writes_channel_and_roles(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = AdminCtx(
            bot=bot,
            guild_id=1,
            actor_id=2,
            guild=SimpleNamespace(id=1, get_channel=lambda _channel_id: None),
        )
        page = page_by_id("stats")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        assert (await page.ready(ctx)).ok is False
        channel = await page.handle(ctx, "stats.channel", "40")
        assert channel.ok is True
        roles = await page.handle(ctx, "stats.roles", "11 12 13")
        assert roles.ok is True
        async with session_scope(bot.sessions) as session:
            row = await session.get(StatsBoard, 1)
        assert row is not None
        assert row.channel_id == 40
        assert row.roles_json == "[11, 12, 13]"
        posted = await page.handle(ctx, "stats.post", "")
        assert posted.ok is False
        assert posted.error == copy.ADMIN_BAD_CHANNEL
    finally:
        await engine.dispose()


async def test_polls_page_writes_channel_and_role(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("polls")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        assert (await page.ready(ctx)).ok is False
        channel = await page.handle(ctx, "polls.channel", "55")
        assert channel.ok is True
        role = await page.handle(ctx, "polls.role", "77")
        assert role.ok is True
        async with session_scope(bot.sessions) as session:
            row = await session.get(PollSettings, 1)
        assert row is not None
        assert row.channel_id == 55
        assert row.ping_role_id == 77
        assert (await page.ready(ctx)).ok is True
        cleared = await page.handle(ctx, "polls.role", "")
        assert cleared.ok is True
        async with session_scope(bot.sessions) as session:
            row = await session.get(PollSettings, 1)
        assert row is not None
        assert row.ping_role_id is None
    finally:
        await engine.dispose()


async def test_youtube_page_adds_feed_and_role(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("youtube")
        assert page is not None
        _assert_v2(await _render(page, ctx))
        assert (await page.ready(ctx)).ok is False
        added = await page.handle(ctx, "yt.add.set", "UCabcdefghijklmnopqrstuv\n")
        assert added.ok is True
        channel = await page.handle(ctx, "yt.channel", "40")
        assert channel.ok is True
        role = await page.handle(ctx, "yt.role", "9")
        assert role.ok is True
        async with session_scope(bot.sessions) as session:
            row = await session.scalar(select(YoutubeFeed).where(YoutubeFeed.guild_id == 1))
        assert row is not None
        assert row.yt_channel_id == "UCabcdefghijklmnopqrstuv"
        assert row.discord_channel_id == 40
        assert row.ping_role_id == 9
        assert (await page.ready(ctx)).ok is True
        bad = await page.handle(ctx, "yt.add.set", "@handle\n")
        assert bad.ok is False
        assert bad.error == copy.ADMIN_YT_BAD_ID
    finally:
        _YT_DRAFTS.clear()
        await engine.dispose()


def _fake_role(
    role_id: int,
    name: str,
    *,
    position: int = 1,
    managed: bool = False,
    everyone: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=role_id,
        name=name,
        position=position,
        managed=managed,
        is_default=lambda: everyone,
    )


def _pick_guild(*roles: SimpleNamespace, guild_id: int = 1, bot_position: int = 50) -> SimpleNamespace:
    everyone = _fake_role(guild_id, "@everyone", position=0, everyone=True)
    bot_role = _fake_role(99, "bot", position=bot_position)
    all_roles = [everyone, bot_role, *roles]
    by_id = {int(role.id): role for role in all_roles}

    async def create_role(*, name: str, reason: str | None = None) -> SimpleNamespace:
        _ = reason
        created = _fake_role(500, name, position=5)
        all_roles.append(created)
        by_id[500] = created
        return created

    return SimpleNamespace(
        id=guild_id,
        roles=all_roles,
        default_role=everyone,
        me=SimpleNamespace(top_role=bot_role),
        get_role=lambda role_id: by_id.get(int(role_id)),
        get_channel=lambda _channel_id: None,
        create_role=create_role,
    )


def _string_selects(view: object) -> list:
    return [item for item in view.walk_children() if type(item).__name__ == "Select" and getattr(item, "options", None)]


def test_page_control_carries_pick_flags() -> None:
    control = PageControl(
        "role_pick",
        "sr.add",
        "Роль",
        exclude_ids=(1, 2),
        allow_create=True,
        hint_owned=False,
    )
    assert control.exclude_ids == (1, 2)
    assert control.allow_create is True
    assert control.hint_owned is False


async def test_free_roles_drops_everyone_managed_and_above_bot(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        managed = _fake_role(11, "boost", position=4, managed=True)
        above = _fake_role(12, "admin", position=80)
        free = _fake_role(13, "open", position=8)
        guild = _pick_guild(managed, above, free)
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2, guild=guild)
        found = await free_roles(ctx, subsystem=RoleSubsystem.STAFF)
        values = [item[1] for item in found]
        assert "13" in values
        assert "1" not in values
        assert "11" not in values
        assert "12" not in values
        assert "99" not in values
    finally:
        await engine.dispose()


async def test_free_roles_owned_hint_and_hide(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    bot.roles = SimpleNamespace(
        owner_of=lambda _guild_id, role_id, except_subsystem=None: (
            RoleSubsystem.REPUTATION
            if int(role_id) == 21 and except_subsystem is not RoleSubsystem.REPUTATION
            else None
        )
    )
    try:
        owned = _fake_role(21, "rep", position=6)
        free = _fake_role(22, "open", position=7)
        guild = _pick_guild(owned, free)
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2, guild=guild)
        hinted = await free_roles(ctx, subsystem=RoleSubsystem.STAFF, hint_owned=True)
        hidden = await free_roles(ctx, subsystem=RoleSubsystem.STAFF, hint_owned=False)
        hinted_ids = [item[1] for item in hinted]
        assert hinted_ids == ["22", "21"]
        assert hinted[1][2] == copy.admin_role_owned(copy.role_subsystem_label("reputation"))
        assert [item[1] for item in hidden] == ["22"]
    finally:
        await engine.dispose()


async def test_role_pick_paginates_past_discord_limit() -> None:
    options = tuple((f"r{index}", str(index)) for index in range(DISCORD_SELECT_LIMIT + 5))
    control = PageControl("role_pick", "ranks.watcher", "Следящий", options=options)
    view = page_card(
        title="Ранги",
        fields={},
        footnote=None,
        controls=(control,),
        on_control=_noop,
        on_home=_noop,
    )
    selects = _string_selects(view)
    values = [option.value for select in selects for option in select.options]
    assert len(selects) == 2
    assert values == [str(index) for index in range(DISCORD_SELECT_LIMIT + 5)]
    assert all(len(select.options) <= DISCORD_SELECT_LIMIT for select in selects)


async def test_role_pick_empty_is_disabled_and_skips_handle() -> None:
    called: list[tuple[str, str]] = []

    async def on_control(_interaction: object, control_id: str, value: str) -> None:
        called.append((control_id, value))

    view = page_card(
        title="Ранги",
        fields={},
        footnote=None,
        controls=(PageControl("role_pick", "ranks.watcher", "Следящий"),),
        on_control=on_control,
        on_home=_noop,
    )
    selects = _string_selects(view)
    assert len(selects) == 1
    select = selects[0]
    assert select.disabled is True
    assert len(select.options) == 1
    assert select.options[0].label == copy.ADMIN_ROLE_NONE
    assert select.options[0].value == PICK_EMPTY_VALUE
    select._values = [PICK_EMPTY_VALUE]  # type: ignore[attr-defined]
    await select.callback(SimpleNamespace())
    assert called == []


async def test_owned_role_pick_is_rejected(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    bot.roles = SimpleNamespace(
        bind=lambda *_args, **_kwargs: None,
        owner_of=lambda _guild_id, role_id, except_subsystem=None: (
            RoleSubsystem.REPUTATION
            if int(role_id) == 77 and except_subsystem is not RoleSubsystem.REPUTATION
            else None
        ),
    )
    try:
        ctx = _ctx(bot)
        page = page_by_id("ranks")
        assert page is not None
        result = await page.handle(ctx, "ranks.watcher", "77")
        assert result.ok is False
        assert result.error == copy.config_role_taken(copy.role_subsystem_label("reputation"))
        mute = page_by_id("mute")
        assert mute is not None
        muted = await mute.handle(ctx, "mute.role", "77")
        assert muted.ok is False
        assert muted.error == copy.config_role_taken(copy.role_subsystem_label("reputation"))
        ladder = page_by_id("ladder")
        assert ladder is not None
        step = await ladder.handle(ctx, "ladder.guest", "77")
        assert step.ok is False
        assert step.error == copy.config_role_taken(copy.role_subsystem_label("reputation"))
    finally:
        await engine.dispose()


async def test_selfroles_create_option_and_blocked_create(tmp_path: Path) -> None:
    from time import time

    from conductor.domain.create_lock import BUCKET_ROLE, CreateHold

    bot, engine = await _bot(tmp_path)
    _SR_DRAFTS.clear()
    try:
        guild = _pick_guild(_fake_role(10, "open", position=4))
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2, guild=guild)
        page = page_by_id("selfroles")
        assert page is not None
        snap = await page.snapshot(ctx, 1)
        add = next(item for item in snap.controls if item.id == "sr.add")
        assert add.kind == "role_pick"
        assert add.allow_create is True
        assert add.options[0] == (copy.ADMIN_ROLE_CREATE, ROLE_CREATE_VALUE)
        opened = await page.handle(ctx, "sr.add", ROLE_CREATE_VALUE)
        assert opened.modal == "text"
        assert opened.target == "sr.add.create"
        created = await page.handle(ctx, "sr.add.create", "Новая")
        assert created.modal == "self_role"
        bot.create_holds = {(1, BUCKET_ROLE): CreateHold(bucket=BUCKET_ROLE, until=time() + 30, retry_after=30)}
        blocked = await page.handle(ctx, "sr.add.create", "Ещё")
        assert blocked.ok is False
        assert blocked.error is not None
        voice = page_by_id("voice")
        assert voice is not None
        voice_snap = await voice.snapshot(ctx, 1)
        assert {item.id: item.kind for item in voice_snap.controls if item.id in {"voice.hub", "voice.clone"}} == {
            "voice.hub": "channel_pick",
            "voice.clone": "channel_pick",
        }
    finally:
        _SR_DRAFTS.clear()
        await engine.dispose()


def test_every_admin_page_has_keys() -> None:
    known = {item.key for item in KEY_SPECS}
    for page in all_pages():
        assert page.keys, page.id
        assert page.key in known
        assert set(page.keys.values()) <= known


def test_chief_sees_at_least_eighteen_pages() -> None:
    ranks = empty_rank_roles()
    ranks[Rank.CHIEF] = (7,)
    snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    assert len(pages_for(snapshot, member_role_ids=(7,))) >= 18


def test_panel_command_is_keyed() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "conductor" / "cogs" / "admin.py").read_text(
        encoding="utf-8"
    )
    assert "@keyed(ADMIN_PANEL_OPEN)" in source


def test_allow_and_may_use_agree_on_suspension() -> None:
    ranks = empty_rank_roles()
    ranks[Rank.CHIEF] = (7,)
    snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    page = page_by_id("ladder")
    assert page is not None
    from conductor.domain.access import decide

    for suspended in (False, True):
        for is_admin in (False, True):
            for is_dev in (False, True):
                via_decide = decide(
                    snapshot,
                    key=page.key,
                    member_role_ids=(7,),
                    is_administrator=is_admin,
                    is_developer=is_dev,
                    suspended=suspended,
                )
                via_may = may_use(
                    snapshot,
                    page,
                    "ladder.regular_group",
                    member_role_ids=(7,),
                    is_administrator=is_admin,
                    is_developer=is_dev,
                    suspended=suspended,
                )
                assert via_decide.allowed == via_may.allowed


async def test_partial_keys_leave_hidden_controls(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        page = page_by_id("data")
        assert page is not None
        snap = await page.snapshot(ctx, 1)
        ranks = empty_rank_roles()
        ranks[Rank.CHIEF] = (7,)
        snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
        filtered = filter_controls(snapshot, page, snap.controls, member_role_ids=(7,))
        assert filtered.hidden
        assert {item.id for item in filtered.allowed} == {"data.census", "data.ingest"}
        reputation = page_by_id("reputation")
        assert reputation is not None
        rep_snap = await reputation.snapshot(ctx, 1)
        empty = filter_controls(snapshot, reputation, rep_snap.controls, member_role_ids=(7,))
        assert empty.allowed == ()
        assert empty.hidden
    finally:
        await engine.dispose()


async def test_snapshot_controls_resolve_to_key_specs(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    known = {item.key for item in KEY_SPECS}
    try:
        ctx = _ctx(bot)
        for page in all_pages():
            snap = await page.snapshot(ctx, 1)
            for control in snap.controls:
                assert control_key(page, control.id) in known, (page.id, control.id)
    finally:
        await engine.dispose()


def test_ready_report_ok_follows_state() -> None:
    assert ReadyReport(ReadyState.READY).ok is True
    assert ReadyReport(ReadyState.PARTIAL).ok is False
    assert ReadyReport(ReadyState.BLOCKED).ok is False
    assert ReadyReport(ReadyState.OFF).ok is False


def test_member_access_issue_respects_category_and_channel() -> None:
    everyone = SimpleNamespace(
        id=1,
        permissions=SimpleNamespace(view_channel=True, send_messages=True, connect=True),
    )
    role = SimpleNamespace(
        id=2,
        permissions=SimpleNamespace(view_channel=True, send_messages=True, connect=True),
    )
    hidden = SimpleNamespace(view_channel=False, send_messages=None, connect=None)
    opened = SimpleNamespace(view_channel=True, send_messages=None, connect=None)
    category = SimpleNamespace(id=10, category=None, overwrites={2: hidden})
    closed = SimpleNamespace(id=20, category=category, overwrites={})
    allowed = SimpleNamespace(id=21, category=category, overwrites={2: opened})
    channels = {10: category, 20: closed, 21: allowed}
    guild = SimpleNamespace(
        id=1,
        default_role=everyone,
        get_channel=lambda channel_id: channels.get(int(channel_id)),
    )
    denied = member_access_issue(guild, 20, roles=(role,), need=("view_channel",))
    assert denied is not None
    assert denied.code == "member_perms"
    assert member_access_issue(guild, 21, roles=(role,), need=("view_channel",)) is None


def test_issues_card_groups_fix_and_open() -> None:
    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    issue = ReadyIssue(
        "unbound",
        copy.READY_ISSUE_UNBOUND,
        fix_control="voice.hub",
        skeleton_key="hub",
    )
    view = issues_card(
        groups=(("voice", copy.ADMIN_PAGE_VOICE, (issue,)),),
        on_fix=noop,
        on_open=noop,
        on_home=noop,
    )
    text = collected_text(view)
    assert copy.ADMIN_ISSUES_TITLE in text
    assert copy.READY_ISSUE_UNBOUND in text
    assert copy.ADMIN_FIX in text
    assert copy.ADMIN_OPEN_ISSUE in text


@pytest.mark.parametrize("page_id", [page.id for page in all_pages()])
async def test_admin_page_ready_returns_report(tmp_path: Path, page_id: str) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        page = page_by_id(page_id)
        assert page is not None
        report = await page.ready(_ctx(bot))
        assert isinstance(report, ReadyReport)
        for issue in report.issues:
            assert issue.code in copy.READY_ISSUE_LABELS
    finally:
        await engine.dispose()


async def test_voice_ready_blocked_when_hub_channel_missing(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        async with session_scope(bot.sessions) as session:
            session.add(VoiceHub(guild_id=1, channel_id=100))
        ctx = AdminCtx(
            bot=bot,
            guild_id=1,
            actor_id=2,
            guild=SimpleNamespace(id=1, get_channel=lambda _channel_id: None),
        )
        page = page_by_id("voice")
        assert page is not None
        report = await page.ready(ctx)
        assert report.state is ReadyState.BLOCKED
        assert report.ok is False
        assert any(item.code == "channel_missing" for item in report.issues)
    finally:
        await engine.dispose()


def test_every_button_label_names_an_action() -> None:
    """Кнопка называет действие (UI-KIT §1.4): подпись начинается с глагола."""
    import ast
    import re

    from conductor.presentation import copy as texts

    verbs = (
        "Включить", "Выключить", "Изменить", "Настроить", "Переименовать", "Выложить", "Отправить",
        "Обновить", "Собрать", "Сопоставить", "Пересобрать", "Создать", "Выбрать", "Добавить",
        "Убрать", "Забыть", "Выгрузить", "Импорт", "Экспорт", "Перечитать", "Проверить", "Починить",
        "Вернуть", "Назад", "Дальше", "Перепись", "Ввести",
    )
    folder = Path(__file__).resolve().parents[1] / "src" / "conductor" / "services"
    pattern = re.compile(r'PageControl\(\s*\n?\s*"button",\s*\n?\s*[^,]+,\s*\n?\s*copy\.([A-Za-z_0-9]+)')
    seen = 0
    bad: list[str] = []
    for path in sorted(folder.glob("admin_*.py")):
        ast.parse(path.read_text(encoding="utf-8"))
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            label = getattr(texts, name, None)
            if not isinstance(label, str):
                continue
            seen += 1
            if not label.startswith(verbs):
                bad.append(f"{path.name}: {name} = {label!r}")
    assert seen > 10
    assert bad == []


async def test_pages_without_selects_explain_their_buttons() -> None:
    """Страница из одних кнопок обязана иметь пояснение: иначе непонятно, что они делают."""
    import re

    folder = Path(__file__).resolve().parents[1] / "src" / "conductor" / "services"
    bad: list[str] = []
    for path in sorted(folder.glob("admin_*.py")):
        if path.name in {"admin_pages.py", "admin_panel.py"}:
            continue
        src = path.read_text(encoding="utf-8")
        kinds = re.findall(r'PageControl\(\s*\n?\s*"(\w+)"', src)
        if not kinds:
            continue
        if all(kind == "button" for kind in kinds) and "footnote=" not in src:
            bad.append(path.name)
    assert bad == []
