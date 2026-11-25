"""Проверка ключа права на взаимодействии: карточка отказа, без admin_only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import discord

from conductor.presentation import copy
from conductor.presentation.layout import denied, respond

T = TypeVar("T")


def is_guild_admin(user: object) -> bool:
    return isinstance(user, discord.Member) and bool(user.guild_permissions.administrator)


async def allow_key(interaction: discord.Interaction, key: str) -> bool:
    guild = interaction.guild
    user = interaction.user
    if guild is None:
        await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
        return False
    role_ids = tuple(role.id for role in getattr(user, "roles", ()))
    is_admin = bool(getattr(getattr(user, "guild_permissions", None), "administrator", False))
    method = getattr(interaction.client, "decide_access", None)
    if not callable(method):
        await respond(interaction, denied(copy.denied_actors("special"), need=copy.RANK_SPECIAL))
        return False
    decision = await method(
        guild_id=guild.id,
        user_id=user.id,
        member_role_ids=role_ids,
        is_administrator=is_admin,
        key=key,
    )
    if decision.allowed:
        return True
    if decision.deny_copy:
        await respond(interaction, denied(decision.deny_copy))
        return False
    rank_key = decision.need.key if decision.need is not None else "special"
    need = copy.rank_label(rank_key)
    await respond(interaction, denied(copy.denied_actors(rank_key), need=need))
    return False


def keyed(key: str) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Вешает проверку права, не оборачивая callback.

    Обёртка ломала discord.py: аннотации команды тогда считались в globals
    ``access.py``, а не кога — ``Choice`` / ``Literal`` / ``Range`` давали NameError
    и бот не загружал `setup`.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def predicate(interaction: discord.Interaction) -> bool:
            return await allow_key(interaction, key)

        checks = list(getattr(fn, "__discord_app_commands_checks__", []))
        checks.append(predicate)
        fn.__discord_app_commands_checks__ = checks  # type: ignore[attr-defined]
        fn.access_key = key  # type: ignore[attr-defined]
        return fn

    return decorator
