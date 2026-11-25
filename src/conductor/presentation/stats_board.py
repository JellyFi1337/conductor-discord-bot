"""Карточка канала статистики. Без sqlalchemy и без services."""

from __future__ import annotations

from datetime import datetime

from discord import ui

from conductor.domain.stats_board import BoardInputs, voice_hours
from conductor.presentation import copy
from conductor.presentation.layout import panel
from conductor.presentation.text import stamp
from conductor.presentation.tokens import Tone

PANEL_KIND = "st"


def board_card(inputs: BoardInputs, *, updated_at: datetime) -> ui.LayoutView:
    watchers = [copy.stats_board_rank_line(copy.rank_label(item.key), item.count) for item in inputs.ranks]
    watchers.append(copy.pulse_duty(inputs.duty))
    members = [copy.stats_board_people(humans=inputs.humans, bots=inputs.bots, boosts=inputs.boosters)]
    if inputs.roles:
        members.append(copy.STATS_BOARD_ROLES)
        members.extend(copy.stats_board_role_line(item.role_id, item.count) for item in inputs.roles)
    if inputs.channels:
        channels = "\n".join(
            copy.stats_board_channel_line(item.channel_id, item.messages, item.authors) for item in inputs.channels
        )
    else:
        channels = copy.STATS_BOARD_CHANNELS_EMPTY
    voice = [copy.stats_board_voice(voice_hours(inputs.voice_seconds)), copy.stats_board_tickets(inputs.tickets_open)]
    if inputs.gathering_at is not None:
        title = inputs.gathering_title or copy.PULSE_GATHERING_FALLBACK
        voice.append(copy.pulse_gathering(title, stamp(inputs.gathering_at, "R")))
    return panel(
        copy.STATS_BOARD_TITLE,
        tone=Tone.INFO,
        fields={
            copy.STATS_BOARD_WATCHERS: "\n".join(watchers),
            copy.STATS_BOARD_MEMBERS: "\n".join(members),
            copy.STATS_BOARD_CHANNELS: channels,
            copy.STATS_BOARD_VOICE: "\n".join(voice),
        },
        footnote=f"{copy.STATS_BOARD_FOOTER} · {stamp(updated_at, 'R')}",
        timeout=None,
    )
