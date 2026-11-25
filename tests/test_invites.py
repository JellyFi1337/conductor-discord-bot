from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest

from conductor.presentation import copy
from conductor.services.invites import InviteTracker


class _Invite:
    def __init__(self, code: str, uses: int, *, inviter: object | None = None, channel: object | None = None) -> None:
        self.code = code
        self.uses = uses
        self.inviter = inviter
        self.channel = channel
        self.max_uses = None


class _Guild:
    def __init__(self, invites: list[_Invite] | None = None, *, vanity: str | None = None) -> None:
        self.id = 1
        self.vanity_url_code = vanity
        self._invites = invites
        self._fail = invites is None

    async def invites(self) -> list[_Invite]:
        if self._fail:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "forbidden")
        return list(self._invites or [])


@pytest.mark.asyncio
async def test_consume_join_attributes_increased_code() -> None:
    first = _Invite("alpha", 3, inviter=SimpleNamespace(id=9, __str__=lambda self: "Ann"))
    guild = _Guild([first])
    tracker = InviteTracker()
    await tracker.refresh(guild)
    guild._invites = [_Invite("alpha", 4, inviter=SimpleNamespace(id=9, __str__=lambda self: "Ann"))]
    used = await tracker.consume_join(guild)
    assert used is not None
    assert used.code == "alpha"
    assert used.uses == 4
    assert used.inviter_id == 9


@pytest.mark.asyncio
async def test_consume_join_picks_the_code_that_grew() -> None:
    guild = _Guild([_Invite("one", 1), _Invite("two", 5)])
    tracker = InviteTracker()
    await tracker.refresh(guild)
    guild._invites = [_Invite("one", 1), _Invite("two", 6)]
    used = await tracker.consume_join(guild)
    assert used is not None
    assert used.code == "two"
    assert used.inviter_name == copy.UNKNOWN


@pytest.mark.asyncio
async def test_forbidden_invites_fall_back_to_vanity() -> None:
    guild = _Guild(None, vanity="cool")
    tracker = InviteTracker()
    used = await tracker.consume_join(guild)
    assert used is not None
    assert used.code == "cool"
    assert used.vanity is True
    assert used.inviter_name == "vanity"


async def test_no_growth_uses_vanity_invite() -> None:
    guild = _Guild([_Invite("alpha", 1)], vanity="cool")

    async def vanity_invite() -> SimpleNamespace:
        return SimpleNamespace(code="cool", uses=12, max_uses=None)

    guild.vanity_invite = vanity_invite
    tracker = InviteTracker()
    await tracker.refresh(guild)
    used = await tracker.consume_join(guild)
    assert used is not None
    assert used.vanity is True
    assert used.code == "cool"
    assert used.uses == 12


async def test_several_grew_returns_none() -> None:
    guild = _Guild([_Invite("one", 1), _Invite("two", 1)])
    tracker = InviteTracker()
    await tracker.refresh(guild)
    guild._invites = [_Invite("one", 2), _Invite("two", 2)]
    used = await tracker.consume_join(guild)
    assert used is None


async def test_consume_join_lock_serializes_growth() -> None:
    guild = _Guild([_Invite("alpha", 3)])
    tracker = InviteTracker()
    await tracker.refresh(guild)
    guild._invites = [_Invite("alpha", 4)]
    first, second = await asyncio.gather(tracker.consume_join(guild), tracker.consume_join(guild))
    grown = [item for item in (first, second) if item is not None and not item.vanity]
    assert len(grown) == 1
    assert grown[0].code == "alpha"
    leftover = first if first is not grown[0] else second
    assert leftover is None or leftover.vanity is True

