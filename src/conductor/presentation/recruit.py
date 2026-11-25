"""Карточки рекрут-ссылок."""

from __future__ import annotations

import discord
from discord import ui

from conductor.domain.privacy import FORGOTTEN_REASON, FORGOTTEN_USER_ID
from conductor.domain.recruit import IssuedLink, RecruitCard, RecruitListing, RecruitRow
from conductor.presentation import copy
from conductor.presentation.interact import ack, instant
from conductor.presentation.layout import edit_response, listing, panel
from conductor.presentation.tokens import Tone


def owner_label(user_id: int) -> str:
    if int(user_id) == FORGOTTEN_USER_ID:
        return FORGOTTEN_REASON
    return f"<@{int(user_id)}>"


def status_label(*, revoked: bool, invite_deleted: bool) -> str:
    if invite_deleted:
        return copy.RECRUIT_STATUS_DELETED
    if revoked:
        return copy.RECRUIT_STATUS_REVOKED
    return copy.RECRUIT_STATUS_ACTIVE


def listing_line(row: RecruitRow) -> str:
    line = f"{row.code} · {owner_label(row.owner_user_id)} · {row.joined_total} · {row.active} · {row.present}"
    if row.invite_deleted or row.revoked:
        return f"{line} · {status_label(revoked=row.revoked, invite_deleted=row.invite_deleted)}"
    return line


def link_card(issued: IssuedLink) -> ui.LayoutView:
    link = issued.link
    return panel(
        copy.RECRUIT_LINK_TITLE,
        copy.RECRUIT_REUSED if issued.reused else "",
        tone=Tone.OK,
        fields={
            copy.RECRUIT_FIELD_CODE: link.code,
            copy.RECRUIT_FIELD_URL: issued.url,
            copy.RECRUIT_FIELD_OWNER: owner_label(link.owner_user_id),
            copy.RECRUIT_FIELD_CHANNEL: f"<#{link.channel_id}>",
        },
    )


def report_card(card: RecruitCard) -> ui.LayoutView:
    link = card.link
    return panel(
        copy.RECRUIT_CARD_TITLE,
        fields={
            copy.RECRUIT_FIELD_CODE: link.code,
            copy.RECRUIT_FIELD_URL: card.url,
            copy.RECRUIT_FIELD_OWNER: owner_label(link.owner_user_id),
            copy.RECRUIT_FIELD_CHANNEL: f"<#{link.channel_id}>",
            copy.RECRUIT_FIELD_JOINED: card.summary.joined_total,
            copy.RECRUIT_FIELD_ACTIVE: card.summary.active,
            copy.RECRUIT_FIELD_PRESENT: card.summary.present,
            copy.RECRUIT_FIELD_STATUS: status_label(revoked=link.revoked, invite_deleted=link.invite_deleted),
        },
    )


def listing_card(listing_page: RecruitListing) -> ui.LayoutView:
    lines = [listing_line(row) for row in listing_page.rows]
    body = listing(lines, listing_page.page, listing_page.pages)
    rows: tuple[ui.ActionRow, ...] = ()
    if listing_page.pages > 1:
        prev = ui.Button(label="←", style=discord.ButtonStyle.secondary, disabled=listing_page.page <= 1)
        nxt = ui.Button(
            label="→",
            style=discord.ButtonStyle.secondary,
            disabled=listing_page.page >= listing_page.pages,
        )

        async def back(interaction: discord.Interaction) -> None:
            await _flip(interaction, listing_page.page - 1)

        async def forward(interaction: discord.Interaction) -> None:
            await _flip(interaction, listing_page.page + 1)

        prev.callback = instant(back)
        nxt.callback = instant(forward)
        rows = (ui.ActionRow(prev, nxt),)
    return panel(copy.RECRUIT_REPORT_TITLE, body, tone=Tone.INFO, rows=rows)


async def _flip(interaction: discord.Interaction, page: int) -> None:
    if not await ack(interaction):
        return
    guild = interaction.guild
    recruit = getattr(getattr(interaction, "client", None), "recruit", None)
    getter = getattr(recruit, "report_listing", None)
    if guild is None or not callable(getter):
        view = panel(copy.RECRUIT_REPORT_TITLE, copy.STALE_BODY, tone=Tone.MUTED)
    else:
        updated = await getter(guild, page=page)
        view = listing_card(updated)
    await edit_response(interaction, view)
