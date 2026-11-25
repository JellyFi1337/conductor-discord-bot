from types import SimpleNamespace

import pytest

from conductor.domain.traffic import (
    AIMD_CAPS,
    INVALID_BUDGET,
    PROFILES,
    Admit,
    AimdState,
    BucketState,
    Defer,
    FuseLevel,
    Lane,
    ProfileLimits,
    RateProfile,
    Wait,
    admit,
    aimd_advance,
    aimd_cap,
    aimd_drop,
    aimd_spacing,
    damped_rps,
    fuse_level,
    is_interaction_route,
    is_invalid,
    resource_capped,
    route_key,
    seed_allowance,
    suppress_until,
)
from conductor.presentation import copy
from conductor.services.rate_observer import RateObserver


@pytest.mark.parametrize(
    ("method", "path", "template", "major"),
    [
        ("POST", "/api/v10/guilds/1/channels", "POST /guilds/{id}/channels", "1"),
        ("GET", "/guilds/99/members/2", "GET /guilds/{id}/members/{id}", "99"),
        ("POST", "/channels/55/messages", "POST /channels/{id}/messages", "55"),
        (
            "PATCH",
            "/webhooks/77/raw-token-secret/messages/@original",
            "PATCH /webhooks/{id}/{token}/messages/@original",
            "77",
        ),
        (
            "POST",
            "/api/v10/interactions/12/raw-token-secret/callback",
            "POST /interactions/{id}/{token}/callback",
            "",
        ),
        (
            "POST",
            "/webhooks/77/123456/messages",
            "POST /webhooks/{id}/{token}/messages",
            "77",
        ),
    ],
)
def test_route_key_templates_and_major(method: str, path: str, template: str, major: str) -> None:
    assert route_key(method, path) == (template, major)


def test_route_key_strips_interaction_token() -> None:
    template, major = route_key("POST", "/interactions/12/raw-token-secret/callback")
    assert "raw-token-secret" not in template
    assert "raw-token-secret" not in major
    assert "{token}" in template


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("POST /interactions/{id}/{token}/callback", True),
        ("PATCH /webhooks/{id}/{token}/messages/@original", True),
        ("POST /guilds/{id}/channels", False),
        ("GET /users/@me", False),
    ],
)
def test_is_interaction_route(template: str, expected: bool) -> None:
    assert is_interaction_route(template) is expected


@pytest.mark.parametrize(
    ("status", "scope", "expected"),
    [
        (200, "", False),
        (401, "", True),
        (403, "user", True),
        (429, "user", True),
        (429, "global", True),
        (429, "shared", False),
        (429, "", True),
        (500, "", False),
    ],
)
def test_is_invalid_table(status: int, scope: str, expected: bool) -> None:
    assert is_invalid(status, scope) is expected


async def test_observer_log_does_not_keep_interaction_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_debug(event: str, **kwargs: object) -> None:
        seen.append(str(event))
        seen.extend(f"{key}={value}" for key, value in kwargs.items())

    monkeypatch.setattr("conductor.services.rate_observer.log.debug", fake_debug)
    observer = RateObserver(clock=lambda: 1.0)
    token = "raw-token-secret"
    await observer._on_request_end(
        None,
        None,
        SimpleNamespace(
            method="POST",
            url=f"https://discord.com/api/v10/interactions/12/{token}/callback",
            response=SimpleNamespace(status=204, headers={}),
        ),
    )
    blob = " ".join(seen)
    template, major = route_key("POST", f"/interactions/12/{token}/callback")
    assert token not in template
    assert token not in major
    assert token not in blob
    assert "{token}" in blob


def test_traffic_copy_mentions_rps_and_budget() -> None:
    assert "сейчас" in copy.traffic_rps(1, 2)
    assert str(INVALID_BUDGET) in copy.traffic_invalid(3, INVALID_BUDGET)
    assert "глобальных 429" in copy.traffic_global_429(4)
    assert "нет" in copy.traffic_top("429", ())
    assert "выученный" in copy.HOLD_LOCAL
    assert "продолжится" in copy.traffic_wait_progress(12)


def _bucket(*, remaining: int, reset_after: float, at: float = 0.0) -> BucketState:
    return BucketState(
        template="POST /guilds/{id}/channels",
        major="1",
        bucket_hash="x",
        limit=10,
        remaining=remaining,
        reset_after=reset_after,
        scope="user",
        status=200,
        at=at,
    )


def test_admit_table() -> None:
    limits = PROFILES[RateProfile.EDGE]
    assert isinstance(admit(Lane.INTERACTION, _bucket(remaining=0, reset_after=60, at=0), 0.0, limits), Admit)
    assert isinstance(admit(Lane.USER, _bucket(remaining=1, reset_after=60, at=0), 0.0, limits), Admit)
    assert isinstance(admit(Lane.USER, None, 0.0, limits), Admit)
    waited = admit(Lane.USER, _bucket(remaining=0, reset_after=8, at=0), 0.0, limits)
    assert isinstance(waited, Wait)
    assert waited.seconds == pytest.approx(8)
    deferred = admit(Lane.USER, _bucket(remaining=0, reset_after=60, at=0), 0.0, limits)
    assert isinstance(deferred, Defer)
    assert deferred.seconds == pytest.approx(60)
    stale = admit(Lane.USER, _bucket(remaining=0, reset_after=5, at=0), 6.0, limits)
    assert isinstance(stale, Admit)
    background = admit(Lane.BACKGROUND, _bucket(remaining=0, reset_after=1, at=0), 0.0, limits)
    assert isinstance(background, Defer)


def test_aimd_and_fuse_tables() -> None:
    assert seed_allowance("channel") == 50
    assert aimd_cap(RateProfile.EDGE, "channel") == AIMD_CAPS[RateProfile.EDGE]["channel"]
    assert aimd_spacing(50, window=600) == pytest.approx(12.0)
    assert aimd_spacing(1, window=600) == pytest.approx(600.0)
    assert aimd_drop(50) == 25
    assert aimd_drop(1) == 1
    start = AimdState(50, 0.0, None, 0.0, 0, False)
    grown = aimd_advance(start, 600.0, window=600.0, cap=120, demand=0)
    # окно закрылось без спроса — без роста
    assert grown.allowance == 50
    busy = AimdState(50, 0.0, None, 0.0, 40, False)
    grown = aimd_advance(busy, 600.0, window=600.0, cap=120, demand=0)
    assert grown.allowance == 51
    dropped = aimd_advance(start, 10.0, window=600.0, cap=120, hit_429=True, freeze_for=100.0)
    assert dropped.allowance == 25
    assert dropped.frozen_until == pytest.approx(110.0)
    frozen = aimd_advance(dropped, 700.0, window=600.0, cap=120, demand=0)
    assert frozen.allowance == 25
    limits = ProfileLimits(48, 24, 600, 10, 20, 30)
    assert fuse_level(0, limits) is FuseLevel.OK
    assert fuse_level(10, limits) is FuseLevel.SOFT
    assert fuse_level(20, limits) is FuseLevel.THROTTLE
    assert fuse_level(30, limits) is FuseLevel.STOP
    assert damped_rps(50, 0.0, 10.0) == pytest.approx(40.0)
    assert damped_rps(50, 10.0, 10.0) == pytest.approx(40.0)
    assert damped_rps(50, 610.0, 10.0) == pytest.approx(50.0)
    assert suppress_until((1.0, 2.0), 3.0) is None
    assert suppress_until((1.0, 2.0, 3.0), 3.0) == pytest.approx(3603.0)
    assert resource_capped("channel", channel_count=500) is True
    assert resource_capped("role", role_count=249) is False

