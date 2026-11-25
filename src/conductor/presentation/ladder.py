"""Карточка лестницы активности. Без sqlalchemy и без services."""

from __future__ import annotations

from discord import ui

from conductor.domain.ladder import HOUR, LadderCard, LadderPanel, ladder_bar
from conductor.presentation import copy
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Icon, Tone


def me_lines(card: LadderCard) -> list[str]:
    need = card.need
    body = [
        f"**{copy.LADDER_NOW}:** {copy.ladder_step(card.held)}",
        "",
    ]
    if need.messages_need > 0:
        body.append(f"{copy.LADDER_MESSAGES} {ladder_bar(need.messages_have, need.messages_need)}")
    if need.voice_need > 0:
        voice_have, voice_need, voice_unit = _voice_units(need.voice_have, need.voice_need)
        body.append(f"{copy.LADDER_VOICE} {ladder_bar(voice_have, voice_need)} {voice_unit}")
    if need.reactions_need > 0:
        body.append(f"{copy.LADDER_REACTIONS} {ladder_bar(need.reactions_have, need.reactions_need)}")
    if need.tenure_need > 0:
        body.append(copy.ladder_tenure_line(have=need.tenure_have, need=need.tenure_need))
    return body


def me_view(target: str, card: LadderCard) -> ui.LayoutView:
    return panel(
        copy.ladder_title(target),
        me_lines(card),
        tone=Tone.SPECIAL,
        footnote=copy.ladder_footnote(card),
        icon=Icon.STAR,
    )


def config_view(panel_data: LadderPanel) -> ui.LayoutView:
    return panel(
        copy.LADDER_CONFIG_TITLE,
        copy.ladder_config_body(panel_data),
        tone=Tone.INFO,
        footnote=copy.LADDER_CONFIG_FOOTNOTE,
        icon=Icon.STAR,
    )


def _voice_units(have: int, need: int) -> tuple[int, int, str]:
    if need >= HOUR:
        return have // HOUR, max(1, need // HOUR), copy.LADDER_HOURS
    return have // 60, max(1, need // 60 if need else 1), copy.LADDER_MINUTES
