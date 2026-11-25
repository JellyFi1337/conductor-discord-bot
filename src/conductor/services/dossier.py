"""Сборка досье: репутация, лестница, дела, тикеты, наблюдение."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord
from sqlalchemy import select

from conductor.db.models import Ticket, TicketCategory
from conductor.db.session import session_scope
from conductor.domain.access import Rank, decide, resolve_rank
from conductor.domain.dossier import (
    ACTION_KEYS,
    DOSSIER_CASES,
    DOSSIER_NOTES,
    DossierAction,
    DossierCaseLine,
    DossierSnapshot,
    DossierTickets,
    is_anonymous_category,
    visible_actions,
)
from conductor.domain.moderation import CaseKind
from conductor.domain.tickets import TicketState
from conductor.presentation import copy
from conductor.presentation.access import allow_key
from conductor.presentation.dossier import deliver_history, dossier_card, open_action_modal
from conductor.presentation.layout import denied, respond
from conductor.services.access import cached_snapshot


class DossierService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def actor_rank(self, interaction: discord.Interaction) -> Rank:
        guild = interaction.guild
        if guild is None:
            return Rank.NONE
        snapshot = await cached_snapshot(self._bot, guild.id)
        roles = tuple(role.id for role in getattr(interaction.user, "roles", ()) or ())
        return resolve_rank(roles, snapshot.rank_roles)

    async def show(self, interaction: discord.Interaction, target: discord.abc.User) -> None:
        guild = interaction.guild
        if guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        snap = await self.collect(guild, interaction.user, target)
        await respond(interaction, self._card(snap, target))

    def _card(self, snap: DossierSnapshot, target: discord.abc.User) -> discord.ui.LayoutView:
        async def on_action(interaction: discord.Interaction, action: DossierAction) -> None:
            key = ACTION_KEYS[action]
            if not await allow_key(interaction, key):
                return
            await open_action_modal(interaction, target, action)

        async def on_history(interaction: discord.Interaction) -> None:
            if not await allow_key(interaction, ACTION_KEYS[DossierAction.HISTORY]):
                return
            await deliver_history(interaction, target)

        return dossier_card(snap, on_action=on_action, on_history=on_history)

    async def collect(
        self,
        guild: discord.Guild,
        actor: object,
        target: discord.abc.User,
        *,
        now: datetime | None = None,
    ) -> DossierSnapshot:
        moment = now or datetime.now(UTC)
        actor_id = int(getattr(actor, "id", 0) or 0)
        target_id = int(target.id)
        self_view = actor_id == target_id
        target_is_bot = bool(getattr(target, "bot", False))
        snapshot = await cached_snapshot(self._bot, guild.id)
        actor_roles = tuple(role.id for role in getattr(actor, "roles", ()) or ())
        target_roles = tuple(role.id for role in getattr(target, "roles", ()) or ())
        actor_rank = resolve_rank(actor_roles, snapshot.rank_roles)
        target_rank = resolve_rank(target_roles, snapshot.rank_roles)
        is_admin = bool(getattr(getattr(actor, "guild_permissions", None), "administrator", False))
        probe = getattr(self._bot, "is_developer", None)
        is_dev = bool(callable(probe) and probe(actor_id))
        keys = {
            key
            for key in set(ACTION_KEYS.values())
            if decide(
                snapshot,
                key=key,
                member_role_ids=actor_roles,
                is_administrator=is_admin,
                is_developer=is_dev,
            ).allowed
        }
        watch = await self._bot.moderation.active_watch(guild.id, target_id, now=moment)
        actions = visible_actions(
            keys=keys,
            actor=actor_rank,
            target=target_rank,
            self_view=self_view,
            target_is_bot=target_is_bot,
            watching=watch is not None,
        )
        rep = await self._bot.reputation.dossier(guild.id, target_id, now=moment)
        ladder = await self._bot.ladder.card(guild.id, target_id, now=moment)
        cases = await self._bot.moderation.history(guild.id, target_id)
        notes = [item for item in cases if item.kind == CaseKind.NOTE][:DOSSIER_NOTES]
        recent = cases[:DOSSIER_CASES]
        joined = getattr(target, "joined_at", None)
        tenure = max(0, (moment - joined).days) if joined is not None else ladder.tenure_days
        mute_role = await self._bot.moderation.mute_role_id(guild.id)
        muted = bool(mute_role and mute_role in {int(role.id) for role in getattr(target, "roles", ()) or ()})
        role_ids = tuple(int(role.id) for role in getattr(target, "roles", ()) or ())
        lines = ()
        loader = getattr(getattr(self._bot, "self_roles", None), "member_lines", None)
        if callable(loader):
            lines = await loader(guild.id, role_ids)
        lfg_posts = 0
        counter = getattr(getattr(self._bot, "lfg", None), "author_count", None)
        if callable(counter):
            lfg_posts = await counter(guild.id, target_id, days=30, now=moment)
        return DossierSnapshot(
            user_id=target_id,
            name=str(getattr(target, "display_name", None) or target),
            tenure_days=tenure,
            ladder_step=ladder.held.key,
            rep_score=rep.score,
            sinner=bool(rep.sinner),
            tier_name=str(rep.tier_name or ""),
            timeout_until=_timeout_until(target, moment),
            muted=muted,
            watch=watch,
            cases=tuple(_case_line(item) for item in recent),
            notes=tuple(_case_line(item) for item in notes),
            tickets=await self._tickets(guild.id, target_id),
            actions=actions,
            self_view=self_view,
            target_is_bot=target_is_bot,
            self_roles=tuple(lines),
            lfg_posts=int(lfg_posts),
        )

    async def _tickets(self, guild_id: int, user_id: int) -> DossierTickets:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.execute(
                    select(Ticket.number, TicketCategory.name)
                    .join(TicketCategory, TicketCategory.id == Ticket.category_id)
                    .where(
                        Ticket.guild_id == int(guild_id),
                        Ticket.owner_id == int(user_id),
                        Ticket.state != TicketState.CLOSED,
                    )
                )
            ).all()
        names = [str(name) for _number, name in rows if not is_anonymous_category(str(name))]
        return DossierTickets(count=len(names), categories=tuple(names))


def _case_line(item: object) -> DossierCaseLine:
    return DossierCaseLine(
        number=int(item.number),
        kind=str(item.kind),
        actor_id=int(item.created_by),
        created_at=getattr(item, "created_at", None),
        reason=str(getattr(item, "reason", "") or ""),
        revoked_at=getattr(item, "revoked_at", None),
        revoked_by=getattr(item, "revoked_by", None),
    )


def _timeout_until(target: object, now: datetime) -> datetime | None:
    until = getattr(target, "timed_out_until", None) or getattr(target, "communication_disabled_until", None)
    if until is None:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if until <= now:
        return None
    return until
