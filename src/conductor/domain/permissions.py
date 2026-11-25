"""Права бота, без которых каркас и штатные команды не работают. Без Discord и БД."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["REQUIRED_PERMISSIONS", "RequiredPermission", "has_required_permissions"]


@dataclass(frozen=True, slots=True)
class RequiredPermission:
    attr: str
    label: str


REQUIRED_PERMISSIONS: tuple[RequiredPermission, ...] = (
    RequiredPermission("manage_roles", "Manage Roles"),
    RequiredPermission("manage_channels", "Manage Channels"),
    RequiredPermission("ban_members", "Ban Members"),
    RequiredPermission("kick_members", "Kick Members"),
    RequiredPermission("moderate_members", "Moderate Members"),
    RequiredPermission("move_members", "Move Members"),
    RequiredPermission("manage_messages", "Manage Messages"),
    RequiredPermission("view_audit_log", "View Audit Log"),
    RequiredPermission("create_instant_invite", "Create Instant Invite"),
)


def has_required_permissions(permissions: object | None) -> bool:
    if permissions is None:
        return False
    return all(bool(getattr(permissions, item.attr, False)) for item in REQUIRED_PERMISSIONS)
