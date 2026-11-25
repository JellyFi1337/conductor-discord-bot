"""Машинные коды операций с ролями Discord."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["RoleOpError"]


class RoleOpError(StrEnum):
    """Почему брокер не применил роли. Сервисы сравнивают код, не копирайт."""

    NOT_MEMBER = "not_member"
    GUILD_GONE = "guild_gone"
    MISSING_ROLE = "missing_role"
    HIERARCHY = "hierarchy"
    MANAGED = "managed"
    FORBIDDEN = "forbidden"
    HTTP = "http"
    LOCKED = "locked"
