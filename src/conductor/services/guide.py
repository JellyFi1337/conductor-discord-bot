"""Справка: видимые команды по живому снимку прав. Без Discord."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from conductor.domain.access import AccessSnapshot, Rank
from conductor.domain.guide import (
    TAB_MEMO,
    TAB_START,
    GuideCommand,
    tab_entries,
)
from conductor.presentation.copy.guide_catalog import catalog_entries
from conductor.services.access import cached_snapshot


async def load_snapshot(bot: Any, guild_id: int) -> AccessSnapshot:
    return await cached_snapshot(bot, guild_id)


def for_tab(
    snapshot: AccessSnapshot,
    rank: Rank,
    tab: str,
    *,
    developer: bool = False,
    member_role_ids: Collection[int] = (),
) -> tuple[GuideCommand, ...]:
    entries = catalog_entries()
    if tab == TAB_START:
        return ()
    if tab == TAB_MEMO:
        return tab_entries(snapshot, Rank.NONE, TAB_MEMO, entries=entries)
    return tab_entries(
        snapshot,
        rank,
        tab,
        entries=entries,
        developer=developer,
        member_role_ids=member_role_ids,
    )
