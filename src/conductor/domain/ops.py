"""Правила owner-операций: секции wipe, план ролей, реестр Old. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from conductor.domain.loyalty import TIER_PRIORITY, LoyaltyTier

__all__ = [
    "WIPE_STEPS",
    "MassMode",
    "MatchMode",
    "MemberRolePlan",
    "MemberSkip",
    "MemberState",
    "OldImportPlan",
    "OldImportRow",
    "ProtectKind",
    "RoleAudience",
    "RoleSkip",
    "WipeSection",
    "WipeStep",
    "gate_member",
    "has_excluded",
    "has_required",
    "plan_grant_member",
    "plan_old_import",
    "plan_strip_member",
    "protect_ids",
    "registry_after_mass",
    "role_skip",
    "steps_for",
    "wipe_section",
]


class WipeSection(StrEnum):
    ALL = "all"
    TELEMETRY = "telemetry"
    MEMBERS = "members"
    LOYALTY = "loyalty"
    ACCESS = "access"
    VOICE = "voice"
    TICKETS = "tickets"
    MODERATION = "moderation"
    REPUTATION = "reputation"
    LADDER = "ladder"
    ACHIEVEMENTS = "achievements"
    SOCIAL = "social"
    CONFIG = "config"


class RoleSkip(StrEnum):
    EVERYONE = "everyone"
    MANAGED = "managed"
    HIERARCHY = "hierarchy"


class MemberSkip(StrEnum):
    BOT = "bot"
    EMPTY = "empty"
    REQUIRE = "require"
    EXCEPT = "except"
    PROTECT = "protect"
    TIMEOUT = "timeout"
    PENDING = "pending"
    BOOST = "boost"


class MassMode(StrEnum):
    SELECTED = "selected"
    ALL = "all"


class MatchMode(StrEnum):
    ANY = "any"
    ALL = "all"


class ProtectKind(StrEnum):
    NONE = "none"
    STAFF = "staff"
    OLD = "old"
    STAFF_OLD = "staff_old"


@dataclass(frozen=True, slots=True)
class MemberState:
    timed_out: bool = False
    pending: bool = False
    boosting: bool = False


@dataclass(frozen=True, slots=True)
class RoleAudience:
    require: frozenset[int] = frozenset()
    require_mode: MatchMode = MatchMode.ANY
    exclude: frozenset[int] = frozenset()
    exclude_mode: MatchMode = MatchMode.ANY
    protect: frozenset[int] = frozenset()
    skip_timeout: bool = False
    skip_pending: bool = False
    skip_boost: bool = False

    def with_protect(self, role_ids: Iterable[int]) -> RoleAudience:
        extra = frozenset(int(role_id) for role_id in role_ids if role_id)
        if extra == self.protect:
            return self
        return RoleAudience(
            require=self.require,
            require_mode=self.require_mode,
            exclude=self.exclude,
            exclude_mode=self.exclude_mode,
            protect=extra,
            skip_timeout=self.skip_timeout,
            skip_pending=self.skip_pending,
            skip_boost=self.skip_boost,
        )


@dataclass(frozen=True, slots=True)
class WipeStep:
    table: str
    sections: frozenset[WipeSection]
    parent: str | None = None
    fk: str | None = None
    extra_fk: str | None = None


@dataclass(frozen=True, slots=True)
class MemberRolePlan:
    user_id: int
    add: tuple[int, ...]
    remove: tuple[int, ...]
    skip: MemberSkip | None = None

    @property
    def empty(self) -> bool:
        return not self.add and not self.remove


@dataclass(frozen=True, slots=True)
class OldImportRow:
    user_id: int
    tier: LoyaltyTier
    action: str


@dataclass(frozen=True, slots=True)
class OldImportPlan:
    rows: tuple[OldImportRow, ...]
    skipped_bot: int
    skipped_same: int

    @property
    def assignments(self) -> dict[int, LoyaltyTier]:
        return {row.user_id: row.tier for row in self.rows}

    @property
    def added(self) -> int:
        return sum(1 for row in self.rows if row.action == "add")

    @property
    def updated(self) -> int:
        return sum(1 for row in self.rows if row.action == "update")


def _sec(*sections: WipeSection) -> frozenset[WipeSection]:
    return frozenset({WipeSection.ALL, *sections})


def _child(table: str, section: WipeSection, parent: str, fk: str, extra_fk: str | None = None) -> WipeStep:
    return WipeStep(table, _sec(section), parent=parent, fk=fk, extra_fk=extra_fk)


def _table(table: str, *sections: WipeSection) -> WipeStep:
    return WipeStep(table, _sec(*sections))


WIPE_STEPS: tuple[WipeStep, ...] = (
    _child("voice_room_kicks", WipeSection.VOICE, "voice_rooms", "room_id"),
    _child("voice_room_requests", WipeSection.VOICE, "voice_rooms", "room_id"),
    _child("voice_room_lfg", WipeSection.VOICE, "voice_rooms", "room_id"),
    _child("voice_room_merges", WipeSection.VOICE, "voice_rooms", "from_room_id", "to_room_id"),
    _child("giveaway_entries", WipeSection.SOCIAL, "giveaways", "giveaway_id"),
    _child("giveaway_winners", WipeSection.SOCIAL, "giveaways", "giveaway_id"),
    _child("poll_votes", WipeSection.SOCIAL, "polls", "poll_id"),
    _child("gathering_rsvps", WipeSection.SOCIAL, "gatherings", "gathering_id"),
    _child("group_members", WipeSection.SOCIAL, "groups", "group_id"),
    _child("group_invites", WipeSection.SOCIAL, "groups", "group_id"),
    _table("activity_buckets", WipeSection.TELEMETRY),
    _table("member_daily", WipeSection.TELEMETRY),
    _table("channel_daily", WipeSection.TELEMETRY),
    _table("channel_scans", WipeSection.TELEMETRY),
    _table("interaction_edges", WipeSection.TELEMETRY),
    _table("member_channels", WipeSection.TELEMETRY),
    _table("ingested_messages", WipeSection.TELEMETRY),
    _table("ingested_reactions", WipeSection.TELEMETRY),
    _table("voice_sessions", WipeSection.TELEMETRY),
    _table("members", WipeSection.MEMBERS),
    _table("loyalty_registry", WipeSection.LOYALTY),
    _table("loyalty_batches", WipeSection.LOYALTY),
    _table("loyalty_role_events", WipeSection.LOYALTY),
    _table("guild_ranks", WipeSection.ACCESS),
    _table("guild_permissions", WipeSection.ACCESS),
    _table("guild_access_states", WipeSection.ACCESS),
    _table("unofficial_admin_notices", WipeSection.ACCESS),
    _table("watcher_duty", WipeSection.ACCESS),
    _table("watcher_notes", WipeSection.ACCESS),
    _table("watcher_suspensions", WipeSection.ACCESS, WipeSection.MODERATION),
    _table("voice_hubs", WipeSection.VOICE),
    _table("voice_rooms", WipeSection.VOICE),
    _table("rename_state", WipeSection.VOICE, WipeSection.TICKETS),
    _table("ticket_panels", WipeSection.TICKETS),
    _table("ticket_categories", WipeSection.TICKETS),
    _table("ticket_events", WipeSection.TICKETS),
    _table("ticket_transcripts", WipeSection.TICKETS),
    _table("tickets", WipeSection.TICKETS),
    _table("mod_settings", WipeSection.MODERATION),
    _table("mod_cases", WipeSection.MODERATION),
    _table("mod_evidence", WipeSection.MODERATION),
    _table("member_watch", WipeSection.MODERATION),
    _table("automod_hits", WipeSection.MODERATION),
    _table("automod_requests", WipeSection.MODERATION),
    _table("staff_actions", WipeSection.MODERATION),
    _table("mod_intents", WipeSection.MODERATION),
    _table("channel_locks", WipeSection.MODERATION),
    _table("reputation_settings", WipeSection.REPUTATION),
    _table("reputation_tiers", WipeSection.REPUTATION),
    _table("reputation", WipeSection.REPUTATION),
    _table("reputation_events", WipeSection.REPUTATION),
    _table("ladder_settings", WipeSection.LADDER),
    _table("ladder", WipeSection.LADDER),
    _table("achievement_sources", WipeSection.REPUTATION, WipeSection.ACHIEVEMENTS),
    _table("achievement_grants", WipeSection.REPUTATION, WipeSection.ACHIEVEMENTS),
    _table("achievement_progress", WipeSection.REPUTATION, WipeSection.ACHIEVEMENTS),
    _table("achievement_defs", WipeSection.REPUTATION, WipeSection.ACHIEVEMENTS),
    _table("giveaway_settings", WipeSection.SOCIAL),
    _table("giveaways", WipeSection.SOCIAL),
    _table("polls", WipeSection.SOCIAL),
    _table("gatherings", WipeSection.SOCIAL),
    _table("group_hubs", WipeSection.SOCIAL),
    _table("group_archive_shelves", WipeSection.SOCIAL),
    _table("groups", WipeSection.SOCIAL),
    _table("reminders", WipeSection.CONFIG),
    _table("bug_reports", WipeSection.CONFIG),
    _table("pulse_alert_settings", WipeSection.CONFIG),
    _table("verification_settings", WipeSection.CONFIG),
    _child("self_role_options", WipeSection.CONFIG, "self_role_menus", "menu_id"),
    _table("self_role_menus", WipeSection.CONFIG),
    _table("stats_boards", WipeSection.CONFIG),
    _table("poll_settings", WipeSection.CONFIG),
    _table("youtube_feeds", WipeSection.CONFIG),
    _child("lfg_members", WipeSection.SOCIAL, "lfg_posts", "post_id"),
    _table("lfg_posts", WipeSection.SOCIAL),
    _table("lfg_settings", WipeSection.CONFIG),
    _table("recruit_links", WipeSection.CONFIG),
    _table("panels", WipeSection.CONFIG),
    _table("role_changes", WipeSection.CONFIG),
    _table("guild_configs", WipeSection.CONFIG),
    _table("guild_layout", WipeSection.CONFIG),
    _table("rate_profiles", WipeSection.CONFIG),
)


def wipe_section(value: str) -> WipeSection | None:
    try:
        return WipeSection(value)
    except ValueError:
        return None


def steps_for(section: WipeSection) -> tuple[WipeStep, ...]:
    return tuple(step for step in WIPE_STEPS if section in step.sections)


def role_skip(*, is_default: bool, managed: bool, above_bot: bool) -> RoleSkip | None:
    if is_default:
        return RoleSkip.EVERYONE
    if managed:
        return RoleSkip.MANAGED
    if above_bot:
        return RoleSkip.HIERARCHY
    return None


def has_required(held: Iterable[int], wanted: Iterable[int], mode: MatchMode) -> bool:
    need = {int(role_id) for role_id in wanted}
    if not need:
        return True
    have = {int(role_id) for role_id in held}
    if mode is MatchMode.ALL:
        return need <= have
    return bool(have & need)


def has_excluded(held: Iterable[int], wanted: Iterable[int], mode: MatchMode) -> bool:
    need = {int(role_id) for role_id in wanted}
    if not need:
        return False
    have = {int(role_id) for role_id in held}
    if mode is MatchMode.ALL:
        return need <= have
    return bool(have & need)


def protect_ids(kind: ProtectKind, *, staff: Iterable[int], old: Iterable[int]) -> frozenset[int]:
    ids: set[int] = set()
    if kind in {ProtectKind.STAFF, ProtectKind.STAFF_OLD}:
        ids.update(int(role_id) for role_id in staff if role_id)
    if kind in {ProtectKind.OLD, ProtectKind.STAFF_OLD}:
        ids.update(int(role_id) for role_id in old if role_id)
    return frozenset(ids)


def gate_member(
    held: Iterable[int],
    audience: RoleAudience,
    state: MemberState | None = None,
) -> MemberSkip | None:
    flags = state or MemberState()
    if audience.skip_timeout and flags.timed_out:
        return MemberSkip.TIMEOUT
    if audience.skip_pending and flags.pending:
        return MemberSkip.PENDING
    if audience.skip_boost and flags.boosting:
        return MemberSkip.BOOST
    if not has_required(held, audience.require, audience.require_mode):
        return MemberSkip.REQUIRE
    if has_excluded(held, audience.exclude, audience.exclude_mode):
        return MemberSkip.EXCEPT
    if has_excluded(held, audience.protect, MatchMode.ANY):
        return MemberSkip.PROTECT
    return None


def plan_strip_member(
    *,
    user_id: int,
    is_bot: bool,
    have: Iterable[int],
    targets: Iterable[int] | None,
    audience: RoleAudience | None = None,
    state: MemberState | None = None,
) -> MemberRolePlan:
    if is_bot:
        return MemberRolePlan(user_id, (), (), MemberSkip.BOT)
    held = {int(role_id) for role_id in have}
    skip = gate_member(held, audience or RoleAudience(), state)
    if skip is not None:
        return MemberRolePlan(user_id, (), (), skip)
    wanted = held if targets is None else held & {int(role_id) for role_id in targets}
    remove = tuple(sorted(wanted))
    if not remove:
        return MemberRolePlan(user_id, (), (), MemberSkip.EMPTY)
    return MemberRolePlan(user_id, (), remove)


def plan_grant_member(
    *,
    user_id: int,
    is_bot: bool,
    have: Iterable[int],
    targets: Iterable[int],
    audience: RoleAudience | None = None,
    state: MemberState | None = None,
) -> MemberRolePlan:
    if is_bot:
        return MemberRolePlan(user_id, (), (), MemberSkip.BOT)
    held = {int(role_id) for role_id in have}
    skip = gate_member(held, audience or RoleAudience(), state)
    if skip is not None:
        return MemberRolePlan(user_id, (), (), skip)
    add = tuple(sorted(int(role_id) for role_id in targets if int(role_id) not in held))
    if not add:
        return MemberRolePlan(user_id, (), (), MemberSkip.EMPTY)
    return MemberRolePlan(user_id, add, ())


def registry_after_mass(
    *,
    current: LoyaltyTier | None,
    add_tiers: Iterable[LoyaltyTier],
    remove_tiers: Iterable[LoyaltyTier],
    strip_all: bool,
) -> LoyaltyTier | None:
    if strip_all:
        return None
    added = frozenset(add_tiers)
    if added:
        for tier in TIER_PRIORITY:
            if tier in added:
                return tier
    if current is not None and current in frozenset(remove_tiers):
        return None
    return current


def plan_old_import(
    holders: Sequence[tuple[int, bool]],
    existing: Mapping[int, LoyaltyTier],
    tier: LoyaltyTier,
) -> OldImportPlan:
    rows: list[OldImportRow] = []
    skipped_bot = 0
    skipped_same = 0
    seen: set[int] = set()
    for user_id, is_bot in holders:
        if is_bot:
            skipped_bot += 1
            continue
        person = int(user_id)
        if person in seen:
            continue
        seen.add(person)
        previous = existing.get(person)
        if previous is tier:
            skipped_same += 1
            continue
        rows.append(
            OldImportRow(
                user_id=person,
                tier=tier,
                action="add" if previous is None else "update",
            )
        )
    return OldImportPlan(tuple(rows), skipped_bot, skipped_same)
