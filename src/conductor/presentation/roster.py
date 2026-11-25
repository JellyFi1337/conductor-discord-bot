"""Постраничный список людей без пинга: превью, страницы и файл."""

from __future__ import annotations

from collections.abc import Sequence

import discord
from discord import ui

from conductor.domain.discord_limits import COMPONENTS_PER_MESSAGE
from conductor.presentation import copy
from conductor.presentation.interact import instant
from conductor.presentation.layout import Card, card_file, panel, respond, update_message
from conductor.presentation.tokens import Tone

ROSTER_PAGE = 40

__all__ = [
    "ROSTER_PAGE",
    "RosterView",
    "roster_page",
    "roster_preview",
]


def roster_preview(user_ids: Sequence[int], total: int, limit: int = 10) -> str:
    """Первые limit упоминаний и «и ещё N», без AllowedMentions."""
    shown = [int(user_id) for user_id in list(user_ids)[: max(0, int(limit))]]
    mentions = " ".join(f"<@{user_id}>" for user_id in shown)
    extra = max(0, int(total) - len(shown))
    if extra <= 0:
        return mentions
    suffix = copy.roster_more(extra)
    return f"{mentions} · {suffix}" if mentions else suffix


def roster_page(title: str, user_ids: Sequence[int], page: int, *, total: int | None = None) -> str:
    count = int(total) if total is not None else len(user_ids)
    pages = max(1, (count + ROSTER_PAGE - 1) // ROSTER_PAGE) if count else 1
    current = min(max(1, int(page)), pages)
    mentions = " ".join(f"<@{int(user_id)}>" for user_id in user_ids)
    body = mentions if mentions else copy.ROSTER_EMPTY
    marker = copy.listing_page(current, pages)
    return f"## {title}\n{body}\n-# {marker}"


def _member_name(guild: discord.Guild | None, user_id: int) -> str:
    getter = getattr(guild, "get_member", None)
    member = getter(user_id) if callable(getter) else None
    name = getattr(member, "display_name", None) or getattr(member, "name", None)
    return str(name) if name else str(user_id)


def roster_file_text(user_ids: Sequence[int], guild: discord.Guild | None) -> str:
    lines = [f"{_member_name(guild, int(user_id))} ({int(user_id)})" for user_id in user_ids]
    return "\n".join(lines)


class RosterView(Card):
    """Скрытый постраничный список. Не постоянный, никто не пингуется."""

    def __init__(
        self,
        title: str,
        user_ids: Sequence[int],
        *,
        guild: discord.Guild | None = None,
        download: bool = True,
    ) -> None:
        super().__init__(timeout=600, mentions=discord.AllowedMentions.none())
        self.title = title
        self.user_ids = [int(user_id) for user_id in user_ids]
        self.guild = guild
        self.download = bool(download)
        self.page = 0
        self._mount()

    @property
    def pages(self) -> int:
        if not self.user_ids:
            return 1
        return max(1, (len(self.user_ids) + ROSTER_PAGE - 1) // ROSTER_PAGE)

    def turn(self, delta: int) -> None:
        self.page = min(max(0, self.page + int(delta)), self.pages - 1)
        self._mount()

    def file_card(self) -> ui.LayoutView:
        payload = roster_file_text(self.user_ids, self.guild)
        return panel(
            self.title,
            files=(card_file(copy.ROSTER_FILE, payload),),
            tone=Tone.MUTED,
            timeout=None,
        )

    def _mount(self) -> None:
        self.clear_items()
        start = self.page * ROSTER_PAGE
        chunk = self.user_ids[start : start + ROSTER_PAGE]
        text = roster_page(self.title, chunk, self.page + 1, total=len(self.user_ids))
        prev_btn = ui.Button(
            label=copy.ROSTER_PREV,
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        next_btn = ui.Button(
            label=copy.ROSTER_NEXT,
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.pages - 1,
        )

        async def go_prev(interaction: discord.Interaction) -> None:
            self.turn(-1)
            await update_message(interaction, self)

        async def go_next(interaction: discord.Interaction) -> None:
            self.turn(1)
            await update_message(interaction, self)

        prev_btn.callback = instant(go_prev)
        next_btn.callback = instant(go_next)
        row = ui.ActionRow(prev_btn, next_btn)
        if self.download:
            down_btn = ui.Button(label=copy.ROSTER_DOWNLOAD, style=discord.ButtonStyle.secondary)

            async def download(interaction: discord.Interaction) -> None:
                await respond(interaction, self.file_card())

            down_btn.callback = instant(download)
            row = ui.ActionRow(prev_btn, next_btn, down_btn)
        container = ui.Container(
            ui.TextDisplay(text),
            ui.Separator(),
            row,
        )
        self.add_item(container)
        if self.total_children_count > COMPONENTS_PER_MESSAGE:
            raise RuntimeError("RosterView собрал больше 40 компонентов")
