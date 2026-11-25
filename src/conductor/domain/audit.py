"""Read-only audit of skeleton bindings, live panels, and orphaned Special."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from conductor.domain.access import Rank

__all__ = [
    "PANEL_LIVE_STATE",
    "BindingIssue",
    "GuildAudit",
    "PanelIssue",
    "audit_guild",
    "live_panels_without_message",
    "missing_bindings",
    "orphaned_special",
]

PANEL_LIVE_STATE = "live"
SPECIAL_RANK = Rank.SPECIAL.key


@dataclass(frozen=True, slots=True)
class BindingIssue:
    key: str
    object_id: int


@dataclass(frozen=True, slots=True)
class PanelIssue:
    kind: str
    channel_id: int
    message_id: int


@dataclass(frozen=True, slots=True)
class GuildAudit:
    guild_id: int
    missing_ids: tuple[BindingIssue, ...]
    live_without_message: tuple[PanelIssue, ...]
    scheme_drift: tuple[str, ...]
    orphaned_special: bool
    special_ever_bound: bool
    special_role_ids: tuple[int, ...]
    discord_checked: bool


def orphaned_special(
    *,
    ever_bound: bool,
    special_role_ids: Sequence[int],
    known_role_ids: frozenset[int] | None = None,
) -> bool:
    """True when Special was bound and the role is gone from storage or Discord."""
    if not ever_bound:
        return False
    ids = tuple(int(item) for item in special_role_ids if int(item) > 0)
    if not ids:
        return True
    if known_role_ids is None:
        return False
    return not any(role_id in known_role_ids for role_id in ids)


def missing_bindings(
    bound: Mapping[str, int],
    known_ids: frozenset[int] | None,
) -> tuple[BindingIssue, ...]:
    if known_ids is None:
        return ()
    found: list[BindingIssue] = []
    for key in sorted(bound):
        object_id = int(bound[key])
        if object_id > 0 and object_id not in known_ids:
            found.append(BindingIssue(key, object_id))
    return tuple(found)


def live_panels_without_message(
    panels: Sequence[tuple[str, int, int, str]],
) -> tuple[PanelIssue, ...]:
    """panels: kind, channel_id, message_id, state."""
    found: list[PanelIssue] = []
    for kind, channel_id, message_id, state in panels:
        if state != PANEL_LIVE_STATE:
            continue
        if int(message_id) > 0:
            continue
        found.append(PanelIssue(str(kind), int(channel_id), int(message_id)))
    return tuple(found)


def audit_guild(
    *,
    guild_id: int,
    bound: Mapping[str, int],
    live_panels: Sequence[tuple[str, int, int, str]],
    scheme_drift: Sequence[str],
    special_ever_bound: bool,
    special_role_ids: Sequence[int],
    known_object_ids: frozenset[int] | None = None,
    known_role_ids: frozenset[int] | None = None,
) -> GuildAudit:
    discord_checked = known_object_ids is not None
    roles = known_role_ids if discord_checked else None
    return GuildAudit(
        guild_id=int(guild_id),
        missing_ids=missing_bindings(bound, known_object_ids),
        live_without_message=live_panels_without_message(live_panels),
        scheme_drift=tuple(scheme_drift),
        orphaned_special=orphaned_special(
            ever_bound=special_ever_bound,
            special_role_ids=special_role_ids,
            known_role_ids=roles,
        ),
        special_ever_bound=bool(special_ever_bound),
        special_role_ids=tuple(int(item) for item in special_role_ids if int(item) > 0),
        discord_checked=discord_checked,
    )
