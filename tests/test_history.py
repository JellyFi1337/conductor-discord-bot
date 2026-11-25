from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import ChannelScan, IngestedMessage, IngestedReaction
from conductor.db.session import dialect_insert, session_scope
from conductor.domain.history import (
    MessageFacts,
    TieKind,
    already_ingested,
    count_links,
    directed_ties,
    emoji_key,
    extend_window,
    partners_for,
    planned_history_walks,
    score_pairs,
    unique_user_ids,
    walk_reached_boundary,
)
from conductor.services.history import HistoryLedger, _insert_ingested_messages, _insert_ingested_reactions


def test_count_links_and_unique_mentions() -> None:
    assert count_links("see https://example.com and http://a.test/x") == 2
    assert unique_user_ids(1, [1, 2, 2, 3], 3, None) == (2, 3)


def test_window_does_not_recount_interior() -> None:
    assert already_ingested(50, 10, 90) is True
    assert already_ingested(5, 10, 90) is False
    assert already_ingested(50, None, None) is False
    assert extend_window(5, 10, 90) == (5, 90)
    assert extend_window(100, 10, 90) == (10, 100)
    assert extend_window(7, None, None) == (7, 7)


def test_directed_ties_skip_self_and_bots() -> None:
    facts = MessageFacts(
        guild_id=1,
        user_id=10,
        channel_id=2,
        message_id=99,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_bot=False,
        attachments=1,
        stickers=0,
        embeds=0,
        links=1,
        mention_ids=(11, 12),
        reply_to_id=11,
    )
    ties = directed_ties(facts)
    assert (10, 11, TieKind.MENTION) in ties
    assert (10, 12, TieKind.MENTION) in ties
    assert (10, 11, TieKind.REPLY) in ties
    assert (
        directed_ties(
            MessageFacts(
                guild_id=1,
                user_id=10,
                channel_id=2,
                message_id=99,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                is_bot=True,
                attachments=0,
                stickers=0,
                embeds=0,
                links=0,
                mention_ids=(11,),
                reply_to_id=11,
            )
        )
        == []
    )


def test_pair_strength_weights_replies_over_reactions() -> None:
    scores = score_pairs(
        [
            (1, 2, TieKind.REPLY, 4),
            (2, 1, TieKind.MENTION, 1),
            (3, 4, TieKind.REACTION, 10),
        ]
    )
    assert scores[0].left_id == 1
    assert scores[0].right_id == 2
    assert scores[0].strength == 4 * 3 + 1 * 2
    assert scores[0].replies == 4
    assert scores[0].mentions == 1
    assert scores[1].strength == 10
    partners = partners_for(scores, 1)
    assert partners[0][0] == 2


def test_caught_up_channel_only_walks_newer_messages() -> None:
    window = (10, 90)
    pending = planned_history_walks(window, caught_up=False)
    assert pending[0].after_id == 90
    assert pending[1].before_id == 10
    done = planned_history_walks(window, caught_up=True)
    assert len(done) == 1
    assert done[0].after_id == 90
    assert done[0].before_id is None
    assert planned_history_walks(None, caught_up=False) == planned_history_walks(None, caught_up=True)
    assert walk_reached_boundary(100, None) is True
    assert walk_reached_boundary(100, 100) is False
    assert walk_reached_boundary(40, 100) is True


def test_ledger_does_not_apply_the_same_message_twice() -> None:
    ledger = HistoryLedger()
    facts = MessageFacts(
        guild_id=1,
        user_id=10,
        channel_id=2,
        message_id=50,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_bot=False,
        attachments=0,
        stickers=0,
        embeds=0,
        links=0,
        mention_ids=(11,),
        reply_to_id=11,
    )
    assert ledger.apply(facts) is True
    assert ledger.apply(facts) is False
    assert ledger.accept_live(1, 2, 50) is False
    assert ledger.accept_live(1, 2, 51) is True
    assert ledger.is_caught_up(1, 2) is False
    ledger.mark_caught_up(1, 2)
    assert ledger.is_caught_up(1, 2) is True
    assert planned_history_walks(ledger.snapshot_window(1, 2), caught_up=True)[0].before_id is None


def test_ledger_skips_duplicate_reactions() -> None:
    ledger = HistoryLedger()
    facts = MessageFacts(
        guild_id=1,
        user_id=10,
        channel_id=2,
        message_id=50,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_bot=False,
        attachments=0,
        stickers=0,
        embeds=0,
        links=0,
        mention_ids=(),
        reply_to_id=None,
    )
    assert ledger.apply(facts) is True
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert ledger.apply_reaction(1, 11, 10, moment, message_id=50, emoji="unicode:👍") is True
    assert ledger.apply_reaction(1, 11, 10, moment, message_id=50, emoji="unicode:👍") is False
    assert ledger.apply_reaction(1, 11, 10, moment, message_id=50, emoji="unicode:❤️") is True
    assert ledger.known_message(1, 50) is True


def test_ledger_pressure_tracks_the_buffer() -> None:
    ledger = HistoryLedger()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(6):
        assert ledger.apply(
            MessageFacts(
                guild_id=1,
                user_id=10,
                channel_id=2,
                message_id=100 + index,
                created_at=now,
                is_bot=False,
                attachments=0,
                stickers=0,
                embeds=0,
                links=0,
                mention_ids=(),
                reply_to_id=None,
            )
        )
    assert ledger.buffered >= 6
    assert ledger.over_capacity(high_water=5)
    assert not ledger.over_capacity(high_water=10_000)


def test_emoji_key_distinguishes_custom_and_unicode() -> None:
    class _Emoji:
        def __init__(self, ident: int | None, name: str) -> None:
            self.id = ident
            self.name = name

    assert emoji_key(_Emoji(99, "wave")) == "custom:99"
    assert emoji_key(_Emoji(None, "👍")) == "unicode:👍"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def test_ingest_on_conflict_compiles_on_postgres() -> None:
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    inserter = dialect_insert(session)
    dialect = postgresql_dialect()
    message_sql = str(
        inserter(IngestedMessage)
        .on_conflict_do_nothing(index_elements=["guild_id", "message_id"])
        .compile(dialect=dialect)
    )
    reaction_sql = str(
        inserter(IngestedReaction)
        .on_conflict_do_nothing(index_elements=["guild_id", "message_id", "source_id", "emoji"])
        .compile(dialect=dialect)
    )
    assert "ON CONFLICT" in message_sql
    assert "ON CONFLICT" in reaction_sql


async def test_insert_ingested_rows_are_idempotent(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        async with session_scope(sessions) as session:
            await _insert_ingested_messages(session, [(1, 2, 3), (1, 2, 3)])
            await _insert_ingested_messages(session, [(1, 2, 3)])
            await _insert_ingested_reactions(session, [(1, 3, 9, "unicode:thumb"), (1, 3, 9, "unicode:thumb")])
            await _insert_ingested_reactions(session, [(1, 3, 9, "unicode:thumb")])
            messages = await session.scalar(select(func.count()).select_from(IngestedMessage))
            reactions = await session.scalar(select(func.count()).select_from(IngestedReaction))
        assert messages == 1
        assert reactions == 1
    finally:
        await engine.dispose()


async def test_ledger_load_keeps_windows_not_every_message(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        async with session_scope(sessions) as session:
            session.add(
                ChannelScan(
                    guild_id=1,
                    channel_id=2,
                    oldest_message_id=10,
                    newest_message_id=90,
                    messages_ingested=3,
                    caught_up=True,
                )
            )
            await _insert_ingested_messages(session, [(1, 2, 10), (1, 2, 50), (1, 2, 90), (1, 2, 200)])
        ledger = HistoryLedger()
        await ledger.load(sessions, 1)
        assert ledger.snapshot_window(1, 2) == (10, 90)
        assert ledger.is_caught_up(1, 2) is True
        assert ledger.known_message(1, 50) is False
        assert ledger.accept_live(1, 2, 50) is False
        assert ledger.accept_live(1, 2, 91) is True
        assert await ledger.recorded_message(sessions, 1, 200) is True
        assert await ledger.recorded_message(sessions, 1, 50) is True
        assert (
            ledger.apply(
                MessageFacts(
                    guild_id=1,
                    user_id=10,
                    channel_id=2,
                    message_id=50,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    is_bot=False,
                    attachments=0,
                    stickers=0,
                    embeds=0,
                    links=0,
                    mention_ids=(),
                    reply_to_id=None,
                )
            )
            is False
        )
    finally:
        await engine.dispose()
