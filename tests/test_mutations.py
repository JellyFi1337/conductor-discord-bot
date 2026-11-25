import discord
import pytest

from conductor.domain.mutations import MutationsLockedError
from conductor.services.mutation_lock import DiscordMutationLock


@pytest.mark.asyncio
async def test_role_methods_stay_gated_through_broker() -> None:
    original_add = discord.Member.add_roles
    original_create = discord.Guild.create_text_channel
    lock = DiscordMutationLock()
    lock.install()
    try:
        assert lock.installed is True
        with pytest.raises(MutationsLockedError, match="add_roles"):
            await discord.Member.add_roles(None)  # type: ignore[misc]
        with pytest.raises(MutationsLockedError, match="remove_roles"):
            await discord.Member.remove_roles(None)  # type: ignore[misc]
        assert discord.Guild.create_text_channel is original_create
    finally:
        lock.uninstall()
    assert discord.Member.add_roles is original_add
    assert discord.Guild.create_text_channel is original_create
    assert lock.installed is False


@pytest.mark.asyncio
async def test_enforcement_keeps_original_role_methods() -> None:
    original_add = discord.Member.add_roles
    original_remove = discord.Member.remove_roles
    lock = DiscordMutationLock()
    lock.install()
    try:
        with pytest.raises(MutationsLockedError):
            await discord.Member.add_roles(None)  # type: ignore[misc]
        assert lock._original(discord.Member, "add_roles") is original_add
        assert lock._original(discord.Member, "remove_roles") is original_remove
    finally:
        lock.uninstall()
    assert discord.Member.add_roles is original_add
