from __future__ import annotations

import discord
import pytest

from conductor.domain.mutations import MutationsLockedError
from conductor.services.mutation_lock import DiscordMutationLock, role_broker_allowed


@pytest.mark.asyncio
async def test_lock_blocks_add_roles_outside_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def spy_add(*_args: object, **_kwargs: object) -> None:
        called.append("add")

    async def spy_remove(*_args: object, **_kwargs: object) -> None:
        called.append("remove")

    monkeypatch.setattr(discord.Member, "add_roles", spy_add)
    monkeypatch.setattr(discord.Member, "remove_roles", spy_remove)
    lock = DiscordMutationLock()
    lock.install()
    try:
        with pytest.raises(MutationsLockedError, match="add_roles"):
            await discord.Member.add_roles(None)  # type: ignore[misc]
        with pytest.raises(MutationsLockedError, match="remove_roles"):
            await discord.Member.remove_roles(None)  # type: ignore[misc]
        assert called == []
        with role_broker_allowed():
            await discord.Member.add_roles(None)  # type: ignore[misc]
            await discord.Member.remove_roles(None)  # type: ignore[misc]
        assert called == ["add", "remove"]
    finally:
        lock.uninstall()
    assert lock.installed is False


@pytest.mark.asyncio
async def test_uninstall_restores_original_role_methods() -> None:
    original_add = discord.Member.add_roles
    original_remove = discord.Member.remove_roles
    lock = DiscordMutationLock()
    lock.install()
    try:
        assert discord.Member.add_roles is not original_add
    finally:
        lock.uninstall()
    assert discord.Member.add_roles is original_add
    assert discord.Member.remove_roles is original_remove
