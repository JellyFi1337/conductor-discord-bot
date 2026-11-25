"""Мастер сопоставления существующих каналов и ролей с каркасом."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import discord
import structlog

from conductor.domain.mapping import (
    MappingDecision,
    MappingRow,
    auto_decisions,
    mapping_row_order,
    rank_candidates,
)
from conductor.domain.seed import (
    ExistingItem,
    SeedMode,
    SeedReport,
    SeedVerdict,
    SkeletonKind,
    entry_by_key,
)
from conductor.presentation import copy
from conductor.presentation.interact import ack
from conductor.presentation.layout import denied, respond, update_message
from conductor.presentation.motion import InteractionTarget
from conductor.presentation.ops import seed_blueprint, seed_done
from conductor.presentation.setup_map import mapping_card, mapping_stale_card
from conductor.services.cache import TTLCache

log = structlog.get_logger("conductor.setup_map")

__all__ = ["DRAFT_TTL", "SetupMapper"]

DRAFT_TTL = 1800.0
OwnerClick = Callable[[discord.Interaction], Any]


class SetupMapper:
    """Черновик живёт в памяти процесса; истечение — собрать заново, не пустой экран."""

    def __init__(self, bot: Any, *, clock: Callable[[], float] | None = None) -> None:
        self._bot = bot
        self._drafts: TTLCache[int, tuple[MappingRow, ...]] = TTLCache(
            ttl=DRAFT_TTL,
            clock=clock,
            name="setup_map",
        )

    def opener(self, *, actor_id: int, with_old: bool = False) -> OwnerClick:
        async def open_map(click: discord.Interaction) -> None:
            await self.open(click, actor_id=actor_id, with_old=with_old)

        return open_map

    async def open(self, click: discord.Interaction, *, actor_id: int, with_old: bool = False) -> None:
        guild = click.guild
        if guild is None:
            return
        if int(click.user.id) != int(actor_id):
            await respond(click, denied(copy.OWNER_DENIED, need=copy.OWNER_NEED))
            return
        rows = await self.build(guild, with_old=with_old)
        self.stash(int(guild.id), rows)
        await update_message(click, self._card(guild, actor_id=actor_id, with_old=with_old, page=1))

    async def build(self, guild: discord.Guild, *, with_old: bool = False) -> tuple[MappingRow, ...]:
        existing = await self._bot.seed.snapshot(guild, with_old=with_old)
        used: set[int] = set()
        parents: dict[str, int] = {}
        built: list[MappingRow] = []
        for key in mapping_row_order(with_old=with_old):
            entry = entry_by_key(key)
            if entry is None:
                continue
            item = existing.get(key, ExistingItem())
            bound = int(item.bound_id) if item.bound_id and item.exists else None
            if bound is not None:
                used.add(bound)
                parents[key] = bound
            parent_id = parents.get(entry.category or "")
            pool = tuple(item for item in _pool(guild, entry.kind) if item[0] not in used)
            ranked = rank_candidates(
                entry,
                pool,
                parent_id=parent_id,
                topics=_topics(guild, entry.kind),
            )
            row = MappingRow(
                key=entry.key,
                label=entry.diagnose_label,
                kind=entry.kind,
                bound_id=bound,
                chosen_id=bound,
                candidates=ranked,
                decision=MappingDecision.KEEP if bound is not None else MappingDecision.CREATE,
            )
            decided = auto_decisions((row,))[0]
            if decided.decision is MappingDecision.BIND and decided.chosen_id is not None:
                used.add(int(decided.chosen_id))
                parents[key] = int(decided.chosen_id)
            built.append(decided)
        return tuple(built)

    def stash(self, guild_id: int, rows: Sequence[MappingRow]) -> None:
        self._drafts.set(int(guild_id), tuple(rows))

    def draft(self, guild_id: int) -> tuple[MappingRow, ...]:
        return self._drafts.get(int(guild_id)) or ()

    def choose(self, guild_id: int, key: str, *, decision: MappingDecision, object_id: int | None) -> None:
        rows = self.draft(guild_id)
        if not rows:
            return
        chosen = object_id if decision is MappingDecision.BIND else None
        if decision is MappingDecision.KEEP:
            current = next((row.bound_id for row in rows if row.key == key), None)
            chosen = current
        updated = tuple(
            replace(row, decision=decision, chosen_id=chosen) if row.key == key else row for row in rows
        )
        self.stash(guild_id, updated)

    def clear(self, guild_id: int) -> None:
        self._drafts.invalidate(int(guild_id))

    async def apply(self, guild: discord.Guild, *, actor_id: int) -> SeedReport:
        rows = self.draft(int(guild.id))
        if not rows:
            return SeedReport(verdict=SeedVerdict.FAILED, notes=(copy.SETUP_MAP_STALE,))
        bind_ids = {
            row.key: int(row.chosen_id)
            for row in rows
            if row.decision is MappingDecision.BIND and row.chosen_id is not None
        }
        create_keys = frozenset(row.key for row in rows if row.decision is MappingDecision.CREATE)
        with_old = any(_is_optional(row.key) for row in rows)
        report = SeedReport(verdict=SeedVerdict.OK)
        if bind_ids:
            report = await self._bot.seed.bind_existing(guild, bind_ids, actor_id=actor_id)
        if create_keys:
            report = await self._bot.seed.run(
                guild,
                actor_id=actor_id,
                with_old=with_old,
                mode=SeedMode.BUILD,
                only_keys=create_keys,
            )
        self.clear(int(guild.id))
        return report

    def _card(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        with_old: bool,
        page: int,
    ) -> discord.ui.LayoutView:
        rows = self.draft(int(guild.id))
        if not rows:
            return mapping_stale_card(on_rebuild=self.opener(actor_id=actor_id, with_old=with_old))

        async def on_choose(
            click: discord.Interaction, key: str, decision: MappingDecision, object_id: int | None
        ) -> None:
            if not await self._same_actor(click, actor_id):
                return
            if not self.draft(int(guild.id)):
                await update_message(
                    click,
                    mapping_stale_card(on_rebuild=self.opener(actor_id=actor_id, with_old=with_old)),
                )
                return
            self.choose(int(guild.id), key, decision=decision, object_id=object_id)
            await update_message(click, self._card(guild, actor_id=actor_id, with_old=with_old, page=page))

        async def on_page(click: discord.Interaction, target: int) -> None:
            if not await self._same_actor(click, actor_id):
                return
            if not self.draft(int(guild.id)):
                await update_message(
                    click,
                    mapping_stale_card(on_rebuild=self.opener(actor_id=actor_id, with_old=with_old)),
                )
                return
            await update_message(click, self._card(guild, actor_id=actor_id, with_old=with_old, page=target))

        async def on_apply(click: discord.Interaction) -> None:
            if not await self._same_actor(click, actor_id):
                return
            if not await ack(click):
                return
            report = await self.apply(guild, actor_id=actor_id)
            if report.verdict is SeedVerdict.FAILED and copy.SETUP_MAP_STALE in report.notes:
                await InteractionTarget(click).edit(
                    mapping_stale_card(on_rebuild=self.opener(actor_id=actor_id, with_old=with_old))
                )
                return
            await InteractionTarget(click).edit(seed_done(report))

        async def on_reset(click: discord.Interaction) -> None:
            if not await self._same_actor(click, actor_id):
                return
            fresh = await self.build(guild, with_old=with_old)
            self.stash(int(guild.id), fresh)
            await update_message(click, self._card(guild, actor_id=actor_id, with_old=with_old, page=1))

        async def on_back(click: discord.Interaction) -> None:
            if not await self._same_actor(click, actor_id):
                return
            plan, existing = await self._bot.seed.catalog(guild, with_old=with_old)
            await update_message(click, seed_blueprint(
                    plan,
                    existing=existing,
                    with_old=with_old,
                    on_map=self.opener(actor_id=actor_id, with_old=with_old),
                ))

        return mapping_card(
            rows,
            page=page,
            with_old=with_old,
            on_choose=on_choose,
            on_page=on_page,
            on_apply=on_apply,
            on_reset=on_reset,
            on_back=on_back,
        )

    async def _same_actor(self, click: discord.Interaction, actor_id: int) -> bool:
        if int(click.user.id) == int(actor_id):
            return True
        await respond(click, denied(copy.OWNER_DENIED, need=copy.OWNER_NEED))
        return False


def _pool(guild: discord.Guild, kind: SkeletonKind) -> tuple[tuple[int, str, int | None], ...]:
    if kind is SkeletonKind.ROLE:
        default_id = getattr(getattr(guild, "default_role", None), "id", None)
        found: list[tuple[int, str, int | None]] = []
        for role in getattr(guild, "roles", ()) or ():
            if bool(getattr(role, "managed", False)):
                continue
            if default_id is not None and int(role.id) == int(default_id):
                continue
            found.append((int(role.id), str(role.name), None))
        return tuple(found)
    if kind is SkeletonKind.CATEGORY:
        return tuple(
            (int(channel.id), str(channel.name), None) for channel in getattr(guild, "categories", ()) or ()
        )
    if kind is SkeletonKind.VOICE:
        return tuple(
            (int(channel.id), str(channel.name), getattr(channel, "category_id", None))
            for channel in getattr(guild, "voice_channels", ()) or ()
        )
    return tuple(
        (int(channel.id), str(channel.name), getattr(channel, "category_id", None))
        for channel in getattr(guild, "text_channels", ()) or ()
    )


def _topics(guild: discord.Guild, kind: SkeletonKind) -> dict[int, str]:
    if kind is not SkeletonKind.TEXT:
        return {}
    found: dict[int, str] = {}
    for channel in getattr(guild, "text_channels", ()) or ():
        topic = str(getattr(channel, "topic", "") or "")
        if topic:
            found[int(channel.id)] = topic
    return found


def _is_optional(key: str) -> bool:
    entry = entry_by_key(key)
    return bool(entry is not None and entry.optional)
