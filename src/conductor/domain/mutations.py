"""Имена областей изменения сервера. Политика «закрыто по умолчанию» снята."""

from __future__ import annotations

from enum import StrEnum


class MutationScope(StrEnum):
    ROLES_LOYALTY = "roles.loyalty"
    ROLES_ACTIVITY = "roles.activity"
    ROLES_REPUTATION = "roles.reputation"
    ROLES_STAFF = "roles.staff"
    ROLES_OWNER = "roles.owner"
    ROLES_LOYALTY_ENFORCEMENT = "roles.loyalty_enforcement"
    ROLES_PULSE = "roles.pulse"
    ROLES_VERIFICATION = "roles.verification"
    ROLES_SELFROLES = "roles.selfroles"
    ROLES_ACHIEVEMENTS = "roles.achievements"
    CHANNELS_VOICE = "channels.voice"
    CHANNELS_TICKETS = "channels.tickets"
    CHANNELS_GROUPS = "channels.groups"
    MEMBERS_MODERATION = "members.moderation"
    MEMBERS_MOVE = "members.move"
    GUILD_CONFIG = "guild.config"


class MutationsLockedError(RuntimeError):
    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"Смена ролей только через брокер: {action}.")
