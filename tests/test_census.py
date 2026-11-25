from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.services.census import CENSUS_BATCH, MemberSnapshot, apply_census


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'census.db'}"


def _member(user_id: int, *, joined: datetime, bot: bool = False) -> MemberSnapshot:
    return MemberSnapshot(
        user_id=user_id,
        username=f"u{user_id}",
        display_name=f"n{user_id}",
        is_bot=bot,
        joined_at=joined,
    )


async def test_first_census_uses_batches_and_sleeps(tmp_path: Path, monkeypatch: object) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    sleeps: list[int] = []

    async def fake_sleep(_delay: float) -> None:
        sleeps.append(1)

    monkeypatch.setattr("conductor.services.census.asyncio.sleep", fake_sleep)
    joined = datetime(2026, 1, 1, tzinfo=UTC)
    members = [_member(index, joined=joined) for index in range(CENSUS_BATCH + 1)]
    try:
        result = await apply_census(
            sessions,
            guild_id=1,
            members=members,
            census_at=None,
            now=joined,
        )
        assert result.created == CENSUS_BATCH + 1
        assert len(sleeps) == 2
    finally:
        await engine.dispose()


async def test_repeat_census_only_updates_changed(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    joined = datetime(2026, 1, 1, tzinfo=UTC)
    members = [_member(1, joined=joined), _member(2, joined=joined)]
    try:
        first = await apply_census(
            sessions,
            guild_id=1,
            members=members,
            census_at=None,
            now=joined,
        )
        assert first.created == 2
        repeat = await apply_census(
            sessions,
            guild_id=1,
            members=members,
            census_at=joined,
            now=joined + timedelta(hours=1),
        )
        assert repeat.created == 0
        assert repeat.updated == 0
        changed = [
            members[0],
            MemberSnapshot(
                user_id=2,
                username="u2",
                display_name="n2",
                is_bot=False,
                joined_at=joined + timedelta(days=1),
            ),
        ]
        again = await apply_census(
            sessions,
            guild_id=1,
            members=changed,
            census_at=joined,
            now=joined + timedelta(days=1),
        )
        assert again.updated == 1
        assert again.created == 0
    finally:
        await engine.dispose()
