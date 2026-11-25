from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conductor.cogs.profile import Profile
from conductor.domain.access import REP_SHOW_OTHER, AccessDecision, AccessSnapshot, Rank, empty_rank_roles
from conductor.domain.achievements import AchievementCard, AchievementLine
from conductor.domain.ladder import DEFAULT_CONFIG, LadderCard, LadderStats, Step, next_need
from conductor.domain.reputation import GrowthWait, RepDossier
from conductor.presentation import copy
from conductor.presentation.layout import COMPONENT_LIMIT, TEXT_LIMIT, collected_text
from conductor.presentation.profile import TAB_ACHIEVEMENTS, TAB_ACTIVITY, TAB_REP, profile_card
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.cache import CacheHub

STAMP = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
JOINED = STAMP - timedelta(days=40)


def _assert_v2(view: object) -> None:
    assert len(collected_text(view)) <= TEXT_LIMIT  # type: ignore[arg-type]
    assert view.total_children_count <= COMPONENT_LIMIT  # type: ignore[attr-defined]


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _dossier(*, sinner: bool = False, score: int = 80) -> RepDossier:
    return RepDossier(
        user_id=3,
        score=score,
        ceiling=100,
        month=(),
        wait=GrowthWait(ready=True, until=None, messages_left=0, voice_seconds_left=0),
        dry_run=False,
        sinner=sinner,
        tier=4,
        tier_name="Уважаемый",
        next_name="Образцовый",
        until_next=10,
    )


def _card() -> LadderCard:
    stats = LadderStats(messages=12, voice_seconds=0)
    need = next_need(Step.GUEST, stats, DEFAULT_CONFIG, now=STAMP, joined_at=JOINED, sinner=False)
    return LadderCard(
        user_id=3,
        held=Step.GUEST,
        earned=Step.GUEST,
        desired=Step.GUEST,
        sinner=False,
        stats=stats,
        need=need,
        tenure_days=40,
    )


def test_profile_tabs_fit_v2() -> None:
    dossier = _dossier()
    card = _card()
    rep = profile_card(
        name="Ann",
        tenure_days=40,
        card=card,
        dossier=dossier,
        tab=TAB_REP,
        on_tab=_noop,
    )
    _assert_v2(rep)
    text = collected_text(rep)
    assert "Ann" in text
    assert "40" in text
    assert copy.LADDER_STEP_GUEST in text
    assert "**80**" in text
    activity = profile_card(
        name="Ann",
        tenure_days=40,
        card=card,
        dossier=dossier,
        tab=TAB_ACTIVITY,
        on_tab=_noop,
    )
    _assert_v2(activity)
    shown = collected_text(activity)
    assert copy.LADDER_MESSAGES in shown
    assert copy.LADDER_NO_DEMOTION in shown
    assert copy.LADDER_VOICE not in shown
    achievements = profile_card(
        name="Ann",
        tenure_days=40,
        card=card,
        dossier=dossier,
        tab=TAB_ACHIEVEMENTS,
        on_tab=_noop,
        achievements=AchievementCard(
            user_id=3,
            lines=(
                AchievementLine(
                    key="fixer",
                    title=copy.achievement_key("fixer"),
                    count=5,
                    stage=2,
                    stage_name="Фиксер 2",
                    next_name="Фиксер 3",
                    have=5,
                    need=10,
                ),
            ),
        ),
    )
    _assert_v2(achievements)
    shown_achieve = collected_text(achievements)
    assert copy.PROFILE_TAB_ACHIEVEMENTS in shown_achieve or "Фиксер" in shown_achieve
    assert "5/10" in shown_achieve


def test_profile_shows_self_role_lines() -> None:
    view = profile_card(
        name="Ann",
        tenure_days=40,
        card=_card(),
        dossier=_dossier(),
        tab=TAB_REP,
        on_tab=_noop,
        role_lines=(copy.selfroles_member_line("Подписки", ["Опросы"]),),
    )
    assert "Подписки" in collected_text(view)
    assert "Опросы" in collected_text(view)


def test_profile_staff_quota_line() -> None:
    view = profile_card(
        name="Ann",
        tenure_days=40,
        card=_card(),
        dossier=_dossier(),
        tab=TAB_REP,
        on_tab=_noop,
        staff_quota="сегодня 3/10 · 0/1",
    )
    text = collected_text(view)
    assert copy.PROFILE_STAFF in text
    assert "сегодня 3/10 · 0/1" in text


class _Response:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, **kwargs: object) -> None:
        self.payloads.append(kwargs)
        self._done = True


async def test_profile_other_without_key_is_denied() -> None:
    async def decide_access(**_kwargs: object) -> AccessDecision:
        return AccessDecision(False, Rank.NONE, need=Rank.WATCHER)

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1, get_member=lambda _user_id: None),
        user=SimpleNamespace(id=2, roles=(), guild_permissions=SimpleNamespace(administrator=False)),
        client=SimpleNamespace(decide_access=decide_access),
        response=_Response(),
    )
    cog = Profile(SimpleNamespace())
    other = SimpleNamespace(id=9, display_name="Ann", joined_at=None)
    await cog.profile.callback(cog, interaction, other)  # type: ignore[arg-type]
    assert interaction.response.payloads
    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert copy.DENIED_TITLE in text
    assert copy.denied_actors("watcher") in text
    assert REP_SHOW_OTHER == "rep.show_other"


class _AckTrace:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.response = _AckResponse(self.order)
        self.guild = SimpleNamespace(id=1)
        self.user = SimpleNamespace(id=2, roles=(), guild_permissions=SimpleNamespace(administrator=False))
        self.client = None

    async def edit_original_response(self, **_kwargs: object) -> None:
        self.order.append("edit_original_response")


class _AckResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def is_done(self) -> bool:
        return "defer" in self._order

    async def defer(self, **_kwargs: object) -> None:
        self._order.append("defer")


async def test_profile_switch_defers_before_load() -> None:
    interaction = _AckTrace()

    async def dossier(_guild_id: int, _user_id: int) -> RepDossier:
        interaction.order.append("work")
        return _dossier()

    async def ladder_card(_guild_id: int, _user_id: int) -> LadderCard:
        return _card()

    async def achievements_profile(_guild_id: int, _user_id: int) -> AchievementCard:
        return AchievementCard(user_id=3, lines=())

    caches = CacheHub()
    snapshot = AccessSnapshot(rank_roles=empty_rank_roles(), rules={}, special_locked=True)
    caches.permissions.set((1, SNAPSHOT_CACHE_KEY), snapshot)
    bot = SimpleNamespace(
        reputation=SimpleNamespace(dossier=dossier),
        ladder=SimpleNamespace(card=ladder_card),
        achievements=SimpleNamespace(profile=achievements_profile),
        caches=caches,
    )
    interaction.client = bot
    cog = Profile(bot)
    target = SimpleNamespace(id=3, display_name="Ann", joined_at=None, roles=())
    await cog._switch(interaction, target, TAB_ACTIVITY)  # type: ignore[arg-type]
    assert interaction.order[0] == "defer"
    assert "work" in interaction.order
    assert interaction.order[-1] == "edit_original_response"
    assert interaction.order.index("defer") < interaction.order.index("work")
    assert interaction.order.index("work") < interaction.order.index("edit_original_response")

