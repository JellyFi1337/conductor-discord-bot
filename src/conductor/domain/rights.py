"""Декларативная матрица overwrites каркаса. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from conductor.domain.seed import SkeletonEntry, SkeletonKind, rights_key

__all__ = [
    "MEMBER_TARGETS",
    "PERMISSIONS",
    "RIGHTS",
    "TARGETS",
    "RightRule",
    "missing_rules",
    "rule_flags",
    "rule_matches",
    "rules_for",
]

TARGETS: frozenset[str] = frozenset({"everyone", "verified", "staff", "chief", "mute", "bot", "owner"})
MEMBER_TARGETS: frozenset[str] = frozenset({"everyone", "verified", "staff", "mute"})
PERMISSIONS: tuple[str, ...] = (
    "view_channel",
    "read_message_history",
    "send_messages",
    "add_reactions",
    "connect",
    "speak",
    "manage_channels",
    "manage_messages",
    "create_instant_invite",
    "move_members",
    "mention_everyone",
)


@dataclass(frozen=True, slots=True)
class RightRule:
    target: str
    view_channel: bool | None = None
    read_message_history: bool | None = None
    send_messages: bool | None = None
    add_reactions: bool | None = None
    connect: bool | None = None
    speak: bool | None = None
    manage_channels: bool | None = None
    manage_messages: bool | None = None
    create_instant_invite: bool | None = None
    move_members: bool | None = None
    mention_everyone: bool | None = None


_SEEN = RightRule(target="verified", view_channel=True, read_message_history=True)
_STAFF_SEEN = RightRule(target="staff", view_channel=True, read_message_history=True)
_HIDDEN = RightRule(target="everyone", view_channel=False)
_BOT = RightRule(
    target="bot",
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    manage_channels=True,
)

RIGHTS: Mapping[str, tuple[RightRule, ...]] = {
    "public": (
        RightRule(target="everyone", view_channel=True, read_message_history=True),
        _BOT,
    ),
    "public_ro": (
        RightRule(
            target="everyone",
            view_channel=True,
            read_message_history=True,
            send_messages=False,
        ),
        _BOT,
    ),
    "public_rw": (
        RightRule(
            target="everyone",
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            add_reactions=False,
        ),
        _BOT,
    ),
    "verified": (
        _HIDDEN,
        _SEEN,
        _STAFF_SEEN,
        _BOT,
    ),
    "staff": (
        _HIDDEN,
        _STAFF_SEEN,
        _BOT,
    ),
    "evidence": (
        _HIDDEN,
        RightRule(
            target="chief",
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            manage_messages=True,
        ),
        _BOT,
    ),
    "group_archive": (
        _HIDDEN,
        RightRule(
            target="chief",
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            add_reactions=False,
        ),
        _BOT,
    ),
}

_EXTRAS: Mapping[str, tuple[RightRule, ...]] = {
    "hub": (RightRule(target="verified", view_channel=True, connect=True, speak=True),),
    "cat_rooms": (
        RightRule(target="verified", view_channel=True, connect=True, speak=True),
        RightRule(target="bot", manage_channels=True, move_members=True),
    ),
    "cat_groups": (RightRule(target="verified", view_channel=True, connect=True),),
}


def rules_for(entry: SkeletonEntry) -> tuple[RightRule, ...]:
    if entry.kind is SkeletonKind.ROLE:
        return ()
    rules = _merge(RIGHTS.get(rights_key(entry), ()), _EXTRAS.get(entry.key, ()))
    return _merge(rules, (_mute_for(entry.kind),))


def rule_flags(rule: RightRule) -> dict[str, bool]:
    return {name: value for name in PERMISSIONS if (value := getattr(rule, name)) is not None}


def rule_matches(rule: RightRule, actual: Mapping[str, bool | None]) -> bool:
    return all(actual.get(name) is wanted for name, wanted in rule_flags(rule).items())


def missing_rules(
    entry: SkeletonEntry,
    actual: Mapping[str, Mapping[str, bool | None]],
) -> tuple[RightRule, ...]:
    """Правила, которые не выполнены. Пустой кортеж — права в порядке."""
    found: list[RightRule] = []
    for rule in rules_for(entry):
        flags = actual.get(rule.target)
        if flags is None:
            continue
        if not rule_matches(rule, flags):
            found.append(rule)
    return tuple(found)


def _mute_for(kind: SkeletonKind) -> RightRule:
    if kind is SkeletonKind.VOICE:
        return RightRule(target="mute", speak=False)
    return RightRule(target="mute", send_messages=False, add_reactions=False)


def _merge(base: tuple[RightRule, ...], extra: tuple[RightRule, ...]) -> tuple[RightRule, ...]:
    by_target: dict[str, RightRule] = {rule.target: rule for rule in base}
    order = [rule.target for rule in base]
    for rule in extra:
        current = by_target.get(rule.target)
        if current is None:
            by_target[rule.target] = rule
            order.append(rule.target)
            continue
        by_target[rule.target] = _overlay(current, rule)
    return tuple(by_target[name] for name in order)


def _overlay(base: RightRule, extra: RightRule) -> RightRule:
    values = {name: getattr(base, name) for name in PERMISSIONS}
    for name in PERMISSIONS:
        wanted = getattr(extra, name)
        if wanted is not None:
            values[name] = wanted
    return RightRule(target=base.target, **values)
