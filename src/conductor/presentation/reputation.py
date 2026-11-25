"""Карточка досье репутации. Без sqlalchemy и без services."""

from __future__ import annotations

from discord import ui

from conductor.domain.reputation import RepDossier, RepPanel, score_bar
from conductor.presentation import copy
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Icon, Tone


def dossier_lines(dossier: RepDossier) -> list[str]:
    name = dossier.tier_name or copy.admin_rep_tier(dossier.tier)
    return [
        score_bar(dossier.score, dossier.ceiling),
        f"**{copy.REP_TIER_LINE}:** {name}",
        f"**{copy.REP_UNTIL}:** {copy.rep_until_line(until_next=dossier.until_next, next_name=dossier.next_name)}",
        "",
        f"**{copy.REP_MONTH}:** {copy.rep_month_line(dossier.month)}",
        f"**{copy.REP_NEXT}:** {copy.rep_next_line(dossier.wait)}",
    ]


def dossier_view(target: str, dossier: RepDossier) -> ui.LayoutView:
    footnote = copy.rep_footnote(dossier.tier, dry_run=dossier.dry_run)
    return panel(
        copy.rep_title(target),
        dossier_lines(dossier),
        tone=Tone.SPECIAL,
        footnote=footnote,
        icon=Icon.STAR,
    )


def config_view(panel_data: RepPanel) -> ui.LayoutView:
    dry = panel_data.config.dry_run
    return panel(
        copy.REP_CONFIG_TITLE,
        copy.rep_config_body(panel_data),
        tone=Tone.INFO,
        footnote=copy.REP_CONFIG_FOOTNOTE if not dry else copy.REP_DRY_RUN,
        icon=Icon.STAR,
    )


def cap_cut_view(user_id: int, plan: object, *, dry_run: bool) -> ui.LayoutView:
    applied = getattr(plan, "applied", 0)
    cut = getattr(plan, "cut", 0)
    score = getattr(plan, "score", 0)
    footnote = copy.REP_DRY_RUN if dry_run else None
    return panel(
        copy.REP_CAP_TITLE,
        copy.rep_cap_body(user_id, applied=applied, cut=cut, score=score),
        tone=Tone.WARN,
        footnote=footnote,
        icon=Icon.WARN,
        timeout=None,
    )


def dry_run_view(user_id: int, plan: object) -> ui.LayoutView:
    applied = getattr(plan, "applied", 0)
    score = getattr(plan, "score", 0)
    return panel(
        copy.REP_DRY_TITLE,
        copy.rep_dry_body(user_id, applied=applied, score=score),
        tone=Tone.MUTED,
        footnote=copy.REP_DRY_RUN,
        timeout=None,
    )


def autoban_view(event: object, *, appeal: str) -> ui.LayoutView:
    number = int(getattr(event, "number", 0) or 0)
    user_id = int(getattr(event, "target_id", 0) or 0)
    created = getattr(event, "created_at", None)
    stamp = int(created.timestamp()) if created is not None else 0
    return panel(
        copy.REP_AUTOBAN_TITLE.format(number=number),
        copy.rep_autoban_body(user_id, score=0),
        tone=Tone.DANGER,
        footnote=copy.REP_APPEAL_FOOT.format(stamp=stamp, appeal=appeal),
        icon=Icon.CASE,
        timeout=None,
    )


def would_ban_view(user_id: int, *, score: int, pardon: bool) -> ui.LayoutView:
    title = copy.REP_WOULD_PARDON_TITLE if pardon else copy.REP_WOULD_BAN_TITLE
    return panel(
        title,
        copy.rep_would_ban_body(user_id, score=score, pardon=pardon),
        tone=Tone.MUTED,
        footnote=copy.REP_DRY_RUN,
        timeout=None,
    )


def pardon_view(result: object) -> ui.LayoutView:
    return panel(
        copy.REP_TITLE.format(user=copy.REP_PARDON_USER),
        copy.rep_pardon_body(
            score=int(getattr(result, "score", 0)),
            unbanned=bool(getattr(result, "unbanned", False)),
            own_ban=bool(getattr(result, "own_ban", False)),
        ),
        tone=Tone.OK,
        icon=Icon.DONE,
    )
