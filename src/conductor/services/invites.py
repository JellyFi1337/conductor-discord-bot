from __future__ import annotations

import asyncio

import discord

from conductor.domain.records import UsedInvite
from conductor.presentation import copy

__all__ = ["InviteTracker", "UsedInvite"]


class InviteTracker:
    """Detect which invite a member used by comparing use counters."""

    def __init__(self) -> None:
        self._uses: dict[int, dict[str, int]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(guild_id), asyncio.Lock())

    async def refresh(self, guild: discord.Guild) -> None:
        async with self._lock(guild.id):
            snapshot = await self._snapshot(guild)
            if snapshot is not None:
                self._uses[guild.id] = snapshot

    async def consume_join(self, guild: discord.Guild) -> UsedInvite | None:
        async with self._lock(guild.id):
            current_invites = await self._fetch(guild)
            if current_invites is None:
                return await self._vanity_used(guild)
            previous = self._uses.get(guild.id, {})
            grown: list[discord.Invite] = []
            mapping: dict[str, int] = {}
            for invite in current_invites:
                uses = invite.uses or 0
                mapping[invite.code] = uses
                if uses > previous.get(invite.code, 0):
                    grown.append(invite)
            self._uses[guild.id] = mapping
            if len(grown) == 1:
                return _used(grown[0])
            if len(grown) > 1:
                return None
            return await self._vanity_used(guild)

    async def _snapshot(self, guild: discord.Guild) -> dict[str, int] | None:
        invites = await self._fetch(guild)
        if invites is None:
            return None
        return {invite.code: invite.uses or 0 for invite in invites}

    async def _fetch(self, guild: discord.Guild) -> list[discord.Invite] | None:
        try:
            return await guild.invites()
        except discord.HTTPException:
            return None

    async def _vanity_used(self, guild: discord.Guild) -> UsedInvite | None:
        fetch = getattr(guild, "vanity_invite", None)
        if callable(fetch):
            try:
                invite = await fetch()
            except discord.HTTPException:
                invite = None
            if invite is not None and getattr(invite, "code", None):
                return UsedInvite(
                    code=str(invite.code),
                    inviter_id=None,
                    inviter_name="vanity",
                    uses=int(getattr(invite, "uses", 0) or 0),
                    max_uses=getattr(invite, "max_uses", None),
                    channel_id=None,
                    vanity=True,
                )
        return self._vanity(guild)

    def codes_for(self, guild_id: int) -> frozenset[str]:
        """Коды приглашений сервера из последнего снимка счётчиков."""
        return frozenset(self._uses.get(int(guild_id), ()))

    def _vanity(self, guild: discord.Guild) -> UsedInvite | None:
        code = guild.vanity_url_code
        if not code:
            return None
        return UsedInvite(
            code=code,
            inviter_id=None,
            inviter_name="vanity",
            uses=0,
            max_uses=None,
            channel_id=None,
            vanity=True,
        )


def _used(invite: discord.Invite) -> UsedInvite:
    return UsedInvite(
        code=invite.code,
        inviter_id=invite.inviter.id if invite.inviter else None,
        inviter_name=str(invite.inviter) if invite.inviter else copy.UNKNOWN,
        uses=invite.uses or 0,
        max_uses=invite.max_uses,
        channel_id=invite.channel.id if invite.channel else None,
    )
