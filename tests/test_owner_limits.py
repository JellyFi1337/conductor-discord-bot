from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from conductor.cogs.ops import Owner
from conductor.domain.create_lock import BUCKET_CHANNEL, HoldSource
from conductor.domain.traffic import BucketState, TrafficSnapshot
from conductor.presentation.ops import limits_body
from conductor.services.create_lock import find_active, remember


class _Guild:
    id = 1


async def test_owner_limits_denied_without_rights(monkeypatch) -> None:
    bot = SimpleNamespace(is_developer=lambda _uid: False)
    cog = Owner(bot)
    captured: list[object] = []

    async def fake_respond(_interaction: object, view: object, **_kwargs: object) -> None:
        captured.append(view)

    monkeypatch.setattr("conductor.cogs.ops.respond", fake_respond)
    interaction = SimpleNamespace(user=SimpleNamespace(id=9), guild=_Guild())
    await cog.limits.callback(cog, interaction)  # type: ignore[arg-type]
    assert captured
    assert find_active(bot, 1, (BUCKET_CHANNEL,), now=11.0) is None


async def test_owner_limits_clears_memory_and_file(tmp_path, monkeypatch) -> None:
    bot = SimpleNamespace(
        is_developer=lambda _uid: True,
        settings=SimpleNamespace(data_dir=tmp_path),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=11)},
        log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
    )
    remember(bot, 1, BUCKET_CHANNEL, 80.0, now=10.0, source=HoldSource.DISCORD)
    cog = Owner(bot)
    captured: dict[str, object] = {}

    def fake_confirm(*, body: str, on_yes, on_no):
        captured["yes"] = on_yes
        captured["body"] = body
        return SimpleNamespace()

    monkeypatch.setattr("conductor.presentation.ops.limits_confirm", fake_confirm)
    monkeypatch.setattr("conductor.presentation.ops.respond", AsyncMock())
    monkeypatch.setattr("conductor.presentation.ops.owner_click", AsyncMock(return_value=True))
    monkeypatch.setattr("conductor.presentation.ops.ack", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "conductor.presentation.ops.InteractionTarget",
        lambda _click: SimpleNamespace(edit=AsyncMock()),
    )
    monkeypatch.setattr("conductor.cogs.ops.log_change", lambda *_a, **_k: captured.setdefault("logged", True))
    interaction = SimpleNamespace(user=SimpleNamespace(id=1), guild=_Guild())
    await cog.limits.callback(cog, interaction)  # type: ignore[arg-type]
    yes = captured["yes"]
    assert callable(yes)
    await yes(SimpleNamespace(user=SimpleNamespace(id=1), response=MagicMock()))
    assert find_active(bot, 1, (BUCKET_CHANNEL,), now=11.0) is None
    assert captured.get("logged") is True


def test_limits_body_shows_traffic_and_busy_buckets() -> None:
    traffic = TrafficSnapshot(
        rps_1s=3,
        rps_60s=40,
        invalid_10m=7,
        global_429_10m=1,
        top_429=("POST /guilds/{id}/channels",),
        top_403=("GET /users/@me",),
        buckets=(
            BucketState(
                template="POST /guilds/{id}/channels",
                major="1",
                bucket_hash="fake",
                limit=10,
                remaining=2,
                reset_after=5.0,
                scope="user",
                status=201,
                at=1.0,
            ),
        ),
    )
    text = limits_body((), now=1.0, traffic=traffic)
    assert "3 сейчас" in text
    assert "40 за минуту" in text
    assert "7 из 10000" in text
    assert "глобальных 429" in text
    assert "POST /guilds/{id}/channels" in text
    assert "remaining 2 из 10" in text
    assert "Самые загруженные ведра Discord" in text
