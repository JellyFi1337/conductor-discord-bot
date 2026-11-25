from __future__ import annotations

from pathlib import Path

from discord import ui

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.domain.mapping import (
    AUTO_BIND_THRESHOLD,
    Candidate,
    MappingDecision,
    MappingRow,
    auto_decisions,
    normalize_name,
    rank_candidates,
)
from conductor.domain.seed import SeedMode, SeedReport, SeedVerdict, SkeletonKind, entry_by_key, skeleton_entries
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.setup_map import ROWS_PER_PAGE, mapping_card, mapping_pages, mapping_stale_card
from conductor.services.seed import GuildSeed
from conductor.services.setup_map import DRAFT_TTL, SetupMapper
from test_seed import _bot, _FakeGuild, _url


def test_normalize_name_strips_emoji_and_yo() -> None:
    assert normalize_name("💬-общий-чат") == normalize_name("Общий Чат")
    assert normalize_name("ёлка") == normalize_name("елка")
    assert normalize_name("Правила  —  сервера") == "правила сервера"


def test_rank_candidates_exact_is_first() -> None:
    entry = entry_by_key("rules")
    assert entry is not None
    pool = (
        (3, "наши-правила", None),
        (1, entry.name, None),
        (2, "правила-сервера", None),
    )
    ranked = rank_candidates(entry, pool)
    assert ranked[0].object_id == 1
    assert ranked[0].score == 1.0
    assert ranked[0].reason == "exact"


def test_rank_candidates_ignores_pool_order() -> None:
    entry = entry_by_key("rules")
    assert entry is not None
    pool = (
        (11, "чат", None),
        (7, entry.name, None),
        (5, "правила-сервера", None),
        (9, "наши правила", None),
    )
    forward = rank_candidates(entry, pool)
    backward = rank_candidates(entry, tuple(reversed(pool)))
    shuffled = rank_candidates(entry, (pool[2], pool[0], pool[3], pool[1]))
    assert forward == backward == shuffled


def test_auto_decisions_ties_stay_with_human() -> None:
    entry = entry_by_key("rules")
    assert entry is not None
    row = MappingRow(
        key=entry.key,
        label=entry.diagnose_label,
        kind=entry.kind,
        bound_id=None,
        chosen_id=None,
        candidates=(
            Candidate(object_id=1, name="a", score=0.9, reason="normalized"),
            Candidate(object_id=2, name="b", score=0.9, reason="normalized"),
        ),
        decision=MappingDecision.CREATE,
    )
    decided = auto_decisions((row,))[0]
    assert decided.decision is not MappingDecision.BIND
    assert decided.chosen_id is None
    assert decided.decision is MappingDecision.CREATE


def test_auto_decisions_skip_when_not_creatable() -> None:
    row = MappingRow(
        key="not_a_skeleton_row",
        label="чужое",
        kind=SkeletonKind.TEXT,
        bound_id=None,
        chosen_id=None,
        candidates=(),
        decision=MappingDecision.CREATE,
    )
    decided = auto_decisions((row,))[0]
    assert decided.decision is MappingDecision.SKIP
    assert decided.decision is not MappingDecision.CREATE


def test_auto_decisions_unique_high_score_binds() -> None:
    entry = entry_by_key("rules")
    assert entry is not None
    row = MappingRow(
        key=entry.key,
        label=entry.diagnose_label,
        kind=entry.kind,
        bound_id=None,
        chosen_id=None,
        candidates=(
            Candidate(object_id=4, name=entry.name, score=1.0, reason="exact"),
            Candidate(object_id=8, name="другое", score=0.6, reason="token"),
        ),
        decision=MappingDecision.CREATE,
    )
    decided = auto_decisions((row,), threshold=AUTO_BIND_THRESHOLD)[0]
    assert decided.decision is MappingDecision.BIND
    assert decided.chosen_id == 4


def test_mapping_pages_cap_rows() -> None:
    rows = tuple(
        MappingRow(
            key=entry.key,
            label=entry.diagnose_label,
            kind=entry.kind,
            bound_id=None,
            chosen_id=None,
            candidates=(),
            decision=MappingDecision.CREATE,
        )
        for entry in skeleton_entries()
    )
    pages = mapping_pages(rows)
    assert pages
    assert all(len(page) <= ROWS_PER_PAGE for page in pages)


def test_mapping_card_stays_within_five_action_rows() -> None:
    rows = tuple(
        MappingRow(
            key=entry.key,
            label=entry.diagnose_label,
            kind=entry.kind,
            bound_id=None,
            chosen_id=None,
            candidates=(Candidate(object_id=1000 + index, name=entry.name, score=0.6, reason="token"),),
            decision=MappingDecision.CREATE,
        )
        for index, entry in enumerate(skeleton_entries()[:12])
    )

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    view = mapping_card(rows, page=1, on_choose=noop, on_page=noop, on_apply=noop, on_reset=noop, on_back=noop)
    container = view.children[0]
    action_rows = [child for child in container.children if isinstance(child, ui.ActionRow)]
    assert len(action_rows) <= 5
    selects = sum(1 for row in action_rows if any(isinstance(item, ui.Select) for item in row.children))
    assert selects <= ROWS_PER_PAGE


def test_stale_card_explains_rebuild() -> None:
    async def noop(_click: object) -> None:
        return None

    view = mapping_stale_card(on_rebuild=noop)
    assert copy.SETUP_MAP_STALE in collected_text(view)


async def test_expired_draft_is_empty(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    bot.seed = GuildSeed(bot)
    now = {"value": 0.0}
    mapper = SetupMapper(bot, clock=lambda: now["value"])
    entry = entry_by_key("rules")
    assert entry is not None
    row = MappingRow(
        key=entry.key,
        label=entry.diagnose_label,
        kind=entry.kind,
        bound_id=None,
        chosen_id=None,
        candidates=(),
        decision=MappingDecision.CREATE,
    )
    try:
        mapper.stash(guild.id, (row,))
        assert mapper.draft(guild.id) == (row,)
        now["value"] = DRAFT_TTL + 1
        assert mapper.draft(guild.id) == ()
        report = await mapper.apply(guild, actor_id=7)
        assert report.verdict is SeedVerdict.FAILED
        assert copy.SETUP_MAP_STALE in report.notes
    finally:
        await engine.dispose()


async def test_apply_runs_only_create_keys(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    seed = GuildSeed(bot)
    bot.seed = seed
    mapper = SetupMapper(bot)
    ran: dict[str, object] = {}

    async def fake_run(*_args: object, **kwargs: object) -> SeedReport:
        ran["only_keys"] = kwargs.get("only_keys")
        ran["mode"] = kwargs.get("mode")
        return SeedReport(verdict=SeedVerdict.OK)

    seed.run = fake_run  # type: ignore[method-assign]
    rules = entry_by_key("rules")
    verify = entry_by_key("verify")
    hub = entry_by_key("hub")
    assert rules is not None and verify is not None and hub is not None
    mapper.stash(
        guild.id,
        (
            MappingRow(rules.key, rules.diagnose_label, rules.kind, None, None, (), MappingDecision.CREATE),
            MappingRow(verify.key, verify.diagnose_label, verify.kind, None, None, (), MappingDecision.SKIP),
            MappingRow(hub.key, hub.diagnose_label, hub.kind, None, 123, (), MappingDecision.BIND),
        ),
    )
    try:
        report = await mapper.apply(guild, actor_id=7)
        assert report.verdict is SeedVerdict.OK
        assert ran["only_keys"] == frozenset({rules.key})
        assert ran["mode"] is SeedMode.BUILD
        assert verify.key not in ran["only_keys"]  # type: ignore[operator]
        assert hub.key not in ran["only_keys"]  # type: ignore[operator]
        assert mapper.draft(guild.id) == ()
    finally:
        await engine.dispose()


async def test_live_server_adopts_without_renaming(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    seed = GuildSeed(bot)
    bot.seed = seed
    mapper = SetupMapper(bot)
    category = await guild.create_category("вход-свой")
    channel = await guild.create_text_channel("наши-правила", category=category)
    edits: list[dict[str, object]] = []
    original = channel.edit

    async def spy_edit(**kwargs: object) -> None:
        edits.append(dict(kwargs))
        await original(**kwargs)

    channel.edit = spy_edit  # type: ignore[method-assign]
    cat_entry = entry_by_key("cat_entry")
    rules = entry_by_key("rules")
    assert cat_entry is not None and rules is not None
    mapper.stash(
        guild.id,
        (
            MappingRow(
                cat_entry.key,
                cat_entry.diagnose_label,
                cat_entry.kind,
                None,
                category.id,
                (),
                MappingDecision.BIND,
            ),
            MappingRow(
                rules.key,
                rules.diagnose_label,
                rules.kind,
                None,
                channel.id,
                (),
                MappingDecision.BIND,
            ),
        ),
    )
    try:
        report = await mapper.apply(guild, actor_id=7)
        assert report.verdict in {SeedVerdict.OK, SeedVerdict.PARTIAL}
        assert category.name == "вход-свой"
        assert channel.name == "наши-правила"
        assert all("name" not in payload for payload in edits)
    finally:
        await engine.dispose()


def test_setup_map_open_copy() -> None:
    assert copy.SETUP_MAP_OPEN == "Сопоставить существующие"
    assert copy.SETUP_MAP_STALE
