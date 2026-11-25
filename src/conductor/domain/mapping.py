"""Подбор существующих каналов и ролей к строкам каркаса. Без Discord и без БД."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from conductor.domain.seed import (
    CHANNEL_PAGES,
    ROLE_GROUPS,
    ROLE_PAGES,
    SkeletonEntry,
    SkeletonKind,
    entries_in_category,
    entry_by_key,
    is_same_name,
    skeleton_entries,
)

__all__ = [
    "AUTO_BIND_THRESHOLD",
    "Candidate",
    "MappingDecision",
    "MappingRow",
    "auto_decisions",
    "mapping_row_order",
    "normalize_name",
    "rank_candidates",
]

AUTO_BIND_THRESHOLD = 0.85
_TOKEN_MIN = 2
_SPACE = re.compile(r"\s+")


class MappingDecision(StrEnum):
    KEEP = "keep"
    BIND = "bind"
    CREATE = "create"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Candidate:
    object_id: int
    name: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class MappingRow:
    key: str
    label: str
    kind: SkeletonKind
    bound_id: int | None
    chosen_id: int | None
    candidates: tuple[Candidate, ...]
    decision: MappingDecision


def normalize_name(raw: str) -> str:
    """casefold, убрать эмодзи, разделители, повторы пробелов и дефисов, ё→е."""
    text = str(raw or "").replace("ё", "е").replace("Ё", "е").casefold()
    chars: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("S") or category in {"Mn", "Mc", "Me", "Cf"}:
            continue
        chars.append(char if char.isalnum() else " ")
    return _SPACE.sub(" ", "".join(chars)).strip()


def rank_candidates(
    entry: SkeletonEntry,
    pool: Sequence[tuple[int, str, int | None]],
    *,
    parent_id: int | None = None,
    topics: Mapping[int, str] | None = None,
    limit: int = 5,
) -> tuple[Candidate, ...]:
    """Пул: (object_id, name, parent_id). Порядок входа на результат не влияет."""
    best: dict[int, Candidate] = {}
    wanted_topic = normalize_name(entry.topic) if entry.topic else ""
    for item in pool:
        object_id, name, item_parent = item[0], item[1], item[2]
        topic = None if topics is None else topics.get(int(object_id))
        score, reason = _score(
            entry,
            name,
            item_parent=item_parent,
            parent_id=parent_id,
            topic=topic,
            wanted_topic=wanted_topic,
        )
        if score <= 0:
            continue
        current = Candidate(object_id=int(object_id), name=str(name), score=score, reason=reason)
        previous = best.get(current.object_id)
        if previous is None or _candidate_key(current) < _candidate_key(previous):
            best[current.object_id] = current
    ranked = tuple(sorted(best.values(), key=_candidate_key))
    if limit < 1:
        return ()
    return ranked[:limit]


def auto_decisions(rows: Sequence[MappingRow], *, threshold: float = AUTO_BIND_THRESHOLD) -> tuple[MappingRow, ...]:
    """Один кандидат ≥ порога — BIND. Два равных высоких — человеку. Иначе CREATE/SKIP."""
    decided: list[MappingRow] = []
    for row in rows:
        if row.bound_id is not None:
            decided.append(replace(row, decision=MappingDecision.KEEP, chosen_id=int(row.bound_id)))
            continue
        strong = tuple(item for item in row.candidates if item.score >= threshold)
        if len(strong) == 1:
            decided.append(replace(row, decision=MappingDecision.BIND, chosen_id=int(strong[0].object_id)))
            continue
        entry = entry_by_key(row.key)
        if entry is not None and entry.creatable:
            decided.append(replace(row, decision=MappingDecision.CREATE, chosen_id=None))
        else:
            decided.append(replace(row, decision=MappingDecision.SKIP, chosen_id=None))
    return tuple(decided)


def mapping_row_order(*, with_old: bool = False) -> tuple[str, ...]:
    """Категории и их дети по CHANNEL_PAGES, затем роли по ROLE_PAGES."""
    keys: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key in seen:
            return
        seen.add(key)
        keys.append(key)

    for cat_keys in CHANNEL_PAGES:
        for cat in cat_keys:
            for entry in entries_in_category(cat, with_old=with_old):
                add(entry.key)
    groups = dict(ROLE_GROUPS)
    allowed = {entry.key for entry in skeleton_entries(with_old=with_old)}
    for group_keys in ROLE_PAGES:
        for group in group_keys:
            for key in groups.get(group, ()):
                if key in allowed:
                    add(key)
    for entry in skeleton_entries(with_old=with_old):
        add(entry.key)
    return tuple(keys)


def _score(
    entry: SkeletonEntry,
    name: str,
    *,
    item_parent: int | None,
    parent_id: int | None,
    topic: str | None,
    wanted_topic: str,
) -> tuple[float, str]:
    score = 0.0
    reason = ""
    if is_same_name(name, entry.name):
        score, reason = 1.0, "exact"
    elif any(is_same_name(name, alias) for alias in entry.aliases):
        score, reason = 0.95, "alias"
    else:
        normalized = normalize_name(name)
        wanted = normalize_name(entry.name)
        if normalized and normalized == wanted:
            score, reason = 0.85, "normalized"
        elif _shared_token(normalized, wanted):
            score, reason = 0.6, "token"
        elif wanted_topic and topic and normalize_name(topic) == wanted_topic:
            score, reason = 0.8, "topic"
    parent_hit = parent_id is not None and item_parent is not None and int(item_parent) == int(parent_id)
    if parent_hit:
        if score > 0:
            score = min(1.0, score + 0.1)
        else:
            score, reason = 0.1, "parent"
    return score, reason


def _shared_token(left: str, right: str) -> bool:
    if not left or not right:
        return False
    wanted = {token for token in right.split() if len(token) >= _TOKEN_MIN}
    if not wanted:
        return False
    return any(token in wanted for token in left.split() if len(token) >= _TOKEN_MIN)


def _candidate_key(item: Candidate) -> tuple[float, str, int]:
    return (-item.score, item.name.casefold(), item.object_id)
