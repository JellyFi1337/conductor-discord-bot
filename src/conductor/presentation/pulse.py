"""Карточка пульса: вкладка «Сейчас». Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Sequence

from discord import ui

from conductor.domain.pulse import HealthBand, PulseAlertRules, PulseSnapshot, public_online
from conductor.presentation import copy
from conductor.presentation.layout import Card, kv, panel
from conductor.presentation.motion import Countdown
from conductor.presentation.text import stamp
from conductor.presentation.tokens import Tone, meter

_BAND_TONE = {
    HealthBand.UNKNOWN: Tone.MUTED,
    HealthBand.QUIET: Tone.MUTED,
    HealthBand.STEADY: Tone.INFO,
    HealthBand.ALIVE: Tone.OK,
    HealthBand.PEAK: Tone.SPECIAL,
}


def alerts_view(rules: PulseAlertRules) -> ui.LayoutView:
    tone = Tone.OK if rules.enabled else Tone.MUTED
    return panel(copy.SIGNAL_TITLE, copy.pulse_alerts_body(rules), tone=tone)


def now_view(
    snapshot: PulseSnapshot,
    *,
    staff: bool,
    rows: Sequence[ui.ActionRow] = (),
) -> ui.LayoutView:
    tone = _BAND_TONE[snapshot.report.band]
    view = Card(timeout=180.0)
    view.add_item(ui.Container(ui.TextDisplay(_public_text(snapshot)), accent_colour=tone.colour))
    if staff:
        view.add_item(ui.Container(ui.TextDisplay(_staff_text(snapshot)), accent_colour=Tone.MUTED.colour))
    if rows:
        view.add_item(ui.Container(*rows, accent_colour=Tone.MUTED.colour))
    return view


def _public_text(snapshot: PulseSnapshot) -> str:
    inputs = snapshot.inputs
    report = snapshot.report
    heading = f"## {tone_mark(report.band)} {copy.PULSE_TITLE}"
    people = copy.pulse_people(
        humans=inputs.humans,
        bots=inputs.bots,
        boosts=inputs.boosters,
        voice=inputs.voice_now,
        online=public_online(inputs),
    )
    health = copy.pulse_health(
        band=copy.pulse_band(report.band.value),
        bar=_score_bar(report.score),
        score=report.score,
    )
    lines = [heading, people, health]
    if inputs.next_gathering_at is not None:
        when = Countdown().render(inputs.next_gathering_at)
        title = inputs.next_gathering_title or copy.PULSE_GATHERING_FALLBACK
        lines.append(copy.pulse_gathering(title, when))
    lines.append(copy.pulse_duty(inputs.duty_watchers))
    if report.band is HealthBand.UNKNOWN:
        lines.append(copy.PULSE_UNKNOWN)
    lines.append(
        f"-# {copy.PULSE_FOOTNOTE} · {copy.stats_snap(stamp(snapshot.assembled_at, 'R'))} · {copy.pulse_zone()}"
    )
    return "\n".join(lines)


def _staff_text(snapshot: PulseSnapshot) -> str:
    inputs = snapshot.inputs
    fields: dict[str, object] = {
        copy.PULSE_STAFF_FLOW: copy.pulse_flow(inputs.joins_7d, inputs.leaves_7d),
        copy.PULSE_STAFF_AUTHORS: inputs.unique_authors_24h,
        copy.PULSE_STAFF_TICKETS: inputs.tickets_open,
        copy.PULSE_STAFF_CENSUS: _census_age(inputs.census_age_hours),
        copy.PULSE_STAFF_INFLOW: copy.pulse_inflow(
            inputs.joins_7d_coded, inputs.joins_7d_vanity, inputs.joins_7d_unknown
        ),
    }
    flags = "\n".join(copy.pulse_flag(flag.value) for flag in snapshot.report.flags)
    body = [f"## {copy.PULSE_STAFF_TITLE}", kv(fields)]
    if flags:
        body.append(flags)
    return "\n".join(body)


def _score_bar(score: int) -> str:
    raw = meter(score, 100)
    return raw.rsplit(" ", 1)[0].rstrip()


def _census_age(hours: float | None) -> str:
    if hours is None:
        return copy.PULSE_CENSUS_NEVER
    if hours < 1:
        return copy.PULSE_CENSUS_FRESH
    days = hours / 24.0
    if days < 1:
        return copy.pulse_census_hours(round(hours))
    return copy.pulse_census_days(round(days))


def tone_mark(band: HealthBand) -> str:
    return _BAND_TONE[band].icon
