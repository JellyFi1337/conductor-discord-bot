"""Коды канала действий следящих. Без Discord и без БД."""

from __future__ import annotations

from conductor.domain.moderation import CaseKind

STAFF_ACTION_MAX = 40
STAFF_DETAILS_MAX = 200
STAFF_REF_MAX = 64

ACTION_TICKET_CLAIM = "ticket.claim"
ACTION_TICKET_CLOSE = "ticket.close"
ACTION_TICKET_APPROVE_REQ = "ticket.approve_req"
ACTION_TICKET_APPROVE = "ticket.approve"
ACTION_TICKET_APPEAL = "ticket.appeal"
ACTION_TICKET_UPHOLD = "ticket.uphold"
ACTION_TICKET_OVERTURN = "ticket.overturn"
ACTION_TICKET_REASON = "ticket.reason"
ACTION_WATCH_APPOINT = "watch.appoint"
ACTION_WATCH_DISMISS = "watch.dismiss"
ACTION_WATCH_DUTY = "watch.duty"
ACTION_WATCH_NOTE = "watch.note"
ACTION_WATCH_SUSPEND = "watcher.suspend"
ACTION_WATCH_LIFT = "watcher.lift"
ACTION_WATCH_SET = "watch.set"
ACTION_WATCH_HIT = "watch.hit"
ACTION_AUTOMOD = "automod.hit"
ACTION_JOIN_SPIKE = "join.spike"
ACTION_QUOTA_DENIED = "quota.denied"
ACTION_BYPASS = "bypass"
ACTION_EXTERNAL = "external_sanction"
ACTION_POLL_REVEAL = "poll.deanonymize"
ACTION_GIVEAWAY_START = "giveaway.start"
ACTION_GUARD_FORGED = "guard.forged"
ACTION_PARDON = "case.pardon"
ACTION_CASE_REVOKE = "case.revoke"
ACTION_LFG_CLOSE = "lfg.close"
ACTION_GROUP_PURGE = "group.purge"

PING_ACTIONS = frozenset({"case.ban", "case.kick", ACTION_EXTERNAL})


def case_action(kind: CaseKind | str) -> str:
    try:
        return f"case.{CaseKind(kind).value}"
    except ValueError:
        return f"case.{kind}"


def sanitize_staff_action(action: str) -> str:
    cleaned = action.strip()[:STAFF_ACTION_MAX]
    return cleaned or "unknown"


def sanitize_staff_details(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    return cleaned[:STAFF_DETAILS_MAX]


def sanitize_staff_ref(ref: int | str | None) -> str | None:
    if ref is None:
        return None
    cleaned = str(ref).strip()[:STAFF_REF_MAX]
    return cleaned or None


def should_ping_chief(action: str, *, ping_chief: bool = False) -> bool:
    return bool(ping_chief) or action in PING_ACTIONS
