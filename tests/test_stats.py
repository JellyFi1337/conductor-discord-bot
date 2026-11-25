from __future__ import annotations

from types import SimpleNamespace

from tests.test_pulse import STAMP, _inputs

from conductor.domain.pulse import PulseSnapshot, health_score
from conductor.presentation import copy
from conductor.presentation.layout import COMPONENT_LIMIT, TEXT_LIMIT, collected_text
from conductor.presentation.stats import (
    TAB_CHANNELS,
    TAB_MEMBERS,
    TAB_NOW,
    TAB_OVERVIEW,
    TAB_TIES,
    channels_fields,
    guild_card,
    guild_ties_fields,
    members_fields,
    overview_fields,
    person_card,
    person_fields,
    person_ties_card,
    person_ties_fields,
)
from conductor.services.history import HistoryCoverage, MemberDossier
from conductor.services.stats import OverviewStats


def _guild() -> SimpleNamespace:
    return SimpleNamespace(
        name="Hall",
        created_at=STAMP,
        get_member=lambda _user_id: None,
        get_channel=lambda _channel_id: None,
        get_thread=lambda _channel_id: None,
    )


def _assert_v2(view: object) -> None:
    assert len(collected_text(view)) <= TEXT_LIMIT  # type: ignore[arg-type]
    assert view.total_children_count <= COMPONENT_LIMIT  # type: ignore[attr-defined]


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _snapshot(*, staff_joins: int = 10) -> PulseSnapshot:
    inputs = _inputs(joins_7d=staff_joins)
    return PulseSnapshot(inputs=inputs, report=health_score(inputs, STAMP), assembled_at=STAMP)


def test_now_tab_hides_staff_without_key() -> None:
    snap = _snapshot()
    public = guild_card(tab=TAB_NOW, snapshot=snap, staff=False, on_tab=_noop)
    text = collected_text(public)
    _assert_v2(public)
    assert copy.PULSE_TITLE in text
    assert copy.PULSE_STAFF_TITLE not in text
    assert copy.stats_snap("")[:6] in text or "снимок" in text
    staff = guild_card(tab=TAB_NOW, snapshot=snap, staff=True, on_tab=_noop)
    staff_text = collected_text(staff)
    _assert_v2(staff)
    assert copy.PULSE_STAFF_TITLE in staff_text


def test_history_tabs_fit_v2() -> None:
    guild = _guild()
    stats = OverviewStats(humans=10, bots=1, boosters=2, left=0, joined_7d=3, joined_30d=4, median_tenure_days=12)
    coverage = HistoryCoverage(channels=2, messages=20, oldest_id=1, newest_id=2)
    overview = guild_card(
        tab=TAB_OVERVIEW,
        title=guild.name,
        fields=overview_fields(guild, census_at=STAMP, coverage=coverage, stats=stats, pairs=()),  # type: ignore[arg-type]
        snapped_at=STAMP,
        on_tab=_noop,
    )
    _assert_v2(overview)
    assert copy.stats_snap("")[:6] in collected_text(overview) or "снимок" in collected_text(overview)
    members = guild_card(
        tab=TAB_MEMBERS,
        title=copy.STATS_TAB_MEMBERS,
        fields=members_fields(guild, stats),  # type: ignore[arg-type]
        snapped_at=STAMP,
        on_tab=_noop,
    )
    _assert_v2(members)
    channels = guild_card(
        tab=TAB_CHANNELS,
        title=copy.STATS_TAB_CHANNELS,
        fields=channels_fields(guild, ((11, 4, 2),)),  # type: ignore[arg-type]
        snapped_at=STAMP,
        on_tab=_noop,
    )
    _assert_v2(channels)
    body, fields = guild_ties_fields(guild, ())  # type: ignore[arg-type]
    ties = guild_card(
        tab=TAB_TIES,
        title=copy.STATS_TIES_TITLE,
        body=body,
        fields=fields,
        snapped_at=STAMP,
        on_tab=_noop,
    )
    _assert_v2(ties)


def test_person_card_and_ties_fit_v2() -> None:
    guild = _guild()
    dossier = MemberDossier(user_id=3, messages=4, attachments=1, stickers=0, links=0)
    card = person_card(str(3), fields=person_fields(guild, dossier), snapped_at=STAMP, on_ties=_noop)  # type: ignore[arg-type]
    _assert_v2(card)
    body, fields = person_ties_fields(guild, (), 3)  # type: ignore[arg-type]
    ties = person_ties_card("user", body=body, fields=fields, snapped_at=STAMP)
    _assert_v2(ties)
    assert copy.STATS_TIES_NONE in collected_text(ties)
