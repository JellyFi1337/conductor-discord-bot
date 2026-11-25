"""Общие проверки процесса, которые сервисам не стоит копировать."""

from __future__ import annotations

from typing import Any

__all__ = ["guild_is_active", "log_tick", "member_on_guild"]


def guild_is_active(bot: object, guild_id: int) -> bool:
    method = getattr(bot, "serves", None)
    if callable(method):
        return bool(method(guild_id))
    return True


def member_on_guild(bot: object, guild_id: int, user_id: int) -> bool:
    """True, если участник сейчас в кэше гильдии."""
    getter = getattr(bot, "get_guild", None)
    guild = getter(guild_id) if callable(getter) else None
    if guild is None:
        return False
    lookup = getattr(guild, "get_member", None)
    if not callable(lookup):
        return False
    return lookup(user_id) is not None


def log_tick(log: Any, event: str, *, changed: int, **fields: object) -> None:
    """Итог периодической задачи: info только если что-то изменилось."""
    payload = {"changed": int(changed), **fields}
    if changed:
        log.info(event, **payload)
        return
    log.debug(event, **payload)
