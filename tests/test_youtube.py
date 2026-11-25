from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiohttp

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import YoutubeFeed
from conductor.db.session import session_scope
from conductor.domain.youtube import (
    DEFAULT_TEMPLATE,
    channel_title,
    new_since,
    parse_feed,
    render,
    valid_channel_id,
)
from conductor.services.youtube import CHANNEL_MISSING, YoutubeService

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "youtube_feed.xml"
CHANNEL_ID = "UCabcdefghijklmnopqrstuv"


class FakeResponse:
    def __init__(self, status: int, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class FakePump:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def submit(self, channel_id: int | None, **kwargs: object) -> None:
        self.items.append({"channel_id": channel_id, **kwargs})


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _bot(
    sessions,
    *,
    session: FakeSession | None = None,
    pump: FakePump | None = None,
    channels: set[int] | None = None,
) -> SimpleNamespace:
    known = {40} if channels is None else channels
    return SimpleNamespace(
        sessions=sessions,
        scheduler=None,
        http_session=session,
        log_pump=pump or FakePump(),
        self_roles=None,
        get_channel=lambda channel_id: SimpleNamespace(id=channel_id) if channel_id in known else None,
    )


def test_valid_channel_id_rejects_handle_and_short_token() -> None:
    assert valid_channel_id(CHANNEL_ID) == CHANNEL_ID
    assert valid_channel_id(f" {CHANNEL_ID} ") == CHANNEL_ID
    assert valid_channel_id("@example") is None
    assert valid_channel_id("UCshort") is None
    assert valid_channel_id("https://www.youtube.com/channel/" + CHANNEL_ID) is None


def test_parse_feed_fixture_keeps_publication_order_helpers() -> None:
    xml = FIXTURE.read_text(encoding="utf-8")
    videos = parse_feed(xml)
    assert channel_title(xml) == "Example Channel"
    assert [video.id for video in videos] == [
        "vid00000005",
        "vid00000004",
        "vid00000003",
        "vid00000002",
        "vid00000001",
    ]
    assert videos[0].url.endswith("/shorts/vid00000005")
    assert videos[-1].title == "Oldest"


def test_new_since_empty_last_id_returns_nothing_and_keeps_newest() -> None:
    videos = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    fresh = new_since(videos, None)
    assert fresh.videos == ()
    assert fresh.last_id == "vid00000005"


def test_skip_shorts_is_off_by_default() -> None:
    videos = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    included = new_since(videos, "vid00000004")
    assert [video.id for video in included.videos] == ["vid00000005"]
    hidden = new_since(videos, "vid00000004", skip_shorts=True)
    assert hidden.videos == ()


def test_new_since_clips_to_three_in_publication_order() -> None:
    videos = parse_feed(FIXTURE.read_text(encoding="utf-8"))
    fresh = new_since(videos, "vid00000001")
    assert [video.id for video in fresh.videos] == ["vid00000002", "vid00000003", "vid00000004"]
    assert fresh.last_id == "vid00000004"
    later = new_since(videos, fresh.last_id)
    assert [video.id for video in later.videos] == ["vid00000005"]


def test_render_template_puts_role_title_and_url() -> None:
    video = parse_feed(FIXTURE.read_text(encoding="utf-8"))[-1]
    text = render(DEFAULT_TEMPLATE, video, "Example Channel", "<@&9>")
    assert text.startswith("<@&9> Новое видео на канале **Example Channel**")
    assert "**Oldest**" in text
    assert video.url in text
    bare = render(DEFAULT_TEMPLATE, video, "Example Channel", "")
    assert not bare.startswith("<@")
    assert bare.startswith("Новое видео")


async def test_network_error_writes_last_error_and_does_not_raise(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    http = FakeSession(error=aiohttp.ClientError("dns"))
    pump = FakePump()
    service = YoutubeService(_bot(sessions, session=http, pump=pump))
    try:
        created = await service.add_feed(
            1,
            yt_channel_id=CHANNEL_ID,
            discord_channel_id=40,
            actor_id=2,
        )
        assert not isinstance(created, str)
        posted = await service.poll_feed(created.id)
        assert posted == 0
        async with session_scope(sessions) as session:
            row = await session.get(YoutubeFeed, created.id)
        assert row is not None
        assert row.last_error == "dns"
        assert pump.items == []
    finally:
        await engine.dispose()


async def test_missing_channel_keeps_cursor_and_reports_error(tmp_path: Path) -> None:
    """Channel gone: the videos must not be skipped silently — cursor stays, health goes red."""
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    xml = FIXTURE.read_text(encoding="utf-8")
    http = FakeSession(FakeResponse(200, xml, {"ETag": '"abc"'}))
    pump = FakePump()
    service = YoutubeService(_bot(sessions, session=http, pump=pump, channels=set()))
    try:
        created = await service.add_feed(1, yt_channel_id=CHANNEL_ID, discord_channel_id=40, actor_id=2)
        assert not isinstance(created, str)
        async with session_scope(sessions) as session:
            stored = await session.get(YoutubeFeed, created.id)
            assert stored is not None
            stored.last_video_id = "vid00000001"
        assert await service.poll_feed(created.id) == 0
        assert pump.items == []
        async with session_scope(sessions) as session:
            row = await session.get(YoutubeFeed, created.id)
        assert row is not None
        assert row.last_video_id == "vid00000001"
        assert row.last_error == CHANNEL_MISSING
    finally:
        await engine.dispose()


async def test_first_poll_remembers_latest_and_later_posts_new(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    xml = FIXTURE.read_text(encoding="utf-8")
    http = FakeSession(FakeResponse(200, xml, {"ETag": '"abc"'}))
    pump = FakePump()
    service = YoutubeService(_bot(sessions, session=http, pump=pump))
    try:
        created = await service.add_feed(
            1,
            yt_channel_id=CHANNEL_ID,
            discord_channel_id=40,
            actor_id=2,
            ping_role_id=9,
        )
        assert not isinstance(created, str)
        assert await service.poll_feed(created.id) == 0
        assert pump.items == []
        async with session_scope(sessions) as session:
            row = await session.get(YoutubeFeed, created.id)
        assert row is not None
        assert row.last_video_id == "vid00000005"
        assert row.yt_channel_title == "Example Channel"
        assert row.last_error is None
        async with session_scope(sessions) as session:
            stored = await session.get(YoutubeFeed, created.id)
            assert stored is not None
            stored.last_video_id = "vid00000001"
        assert await service.poll_feed(created.id) == 3
        assert len(pump.items) == 3
        first = pump.items[0]
        assert first["channel_id"] == 40
        assert first["silent"] is False
        mentions = first["allowed_mentions"]
        assert getattr(mentions, "everyone", True) is False
        assert [role.id for role in mentions.roles] == [9]
        assert "vid00000002" in str(first["content"])
        assert "vid00000005" not in " ".join(str(item["content"]) for item in pump.items)
    finally:
        await engine.dispose()


async def test_add_feed_rejects_invalid_id(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = YoutubeService(_bot(sessions))
    try:
        assert await service.add_feed(1, yt_channel_id="@nope", discord_channel_id=40, actor_id=2) == "bad_id"
    finally:
        await engine.dispose()
