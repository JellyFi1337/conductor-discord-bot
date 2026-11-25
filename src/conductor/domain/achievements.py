"""Достижения: ступени, роли, источники. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

KEYS: tuple[str, ...] = ("fixer", "ideologist", "vigilant")
KEY_STAGES: dict[str, tuple[int, ...]] = {
    "fixer": (1, 5, 10),
    "ideologist": (1, 5, 10),
    "vigilant": (1, 5, 10, 20),
}
SOURCES: dict[tuple[str, str], str] = {
    ("bug", "fixed"): "fixer",
    ("suggestion", "accepted"): "ideologist",
    ("complaint", "sanctioned"): "vigilant",
}
REP_BONUS = 5
KIND_ACHIEVEMENT = "achievement"
NAME_LIMIT = 80


@dataclass(frozen=True, slots=True)
class AchievementDef:
    key: str
    stage: int
    threshold: int
    name: str
    role_id: int | None = None


@dataclass(frozen=True, slots=True)
class AchievementLine:
    key: str
    title: str
    count: int
    stage: int
    stage_name: str | None
    next_name: str | None
    have: int
    need: int


@dataclass(frozen=True, slots=True)
class AchievementCard:
    user_id: int
    lines: tuple[AchievementLine, ...]


@dataclass(frozen=True, slots=True)
class AchievementPanel:
    defs: tuple[AchievementDef, ...]


def parse_key(value: str | None) -> str | None:
    token = str(value or "").strip().lower()
    return token if token in KEYS else None


def key_for_close(kind: object, outcome: object) -> str | None:
    return SOURCES.get((str(kind).strip().lower(), str(outcome).strip().lower()))


def defs_for(key: str, rows: Sequence[AchievementDef]) -> tuple[AchievementDef, ...]:
    return tuple(sorted((row for row in rows if row.key == key), key=lambda item: item.stage))


def default_defs(names: Mapping[str, Sequence[str]] | None = None) -> tuple[AchievementDef, ...]:
    found: list[AchievementDef] = []
    for key, thresholds in KEY_STAGES.items():
        labels = tuple(names.get(key, ())) if names else ()
        for index, threshold in enumerate(thresholds, start=1):
            label = labels[index - 1] if index - 1 < len(labels) else f"{key} {index}"
            found.append(AchievementDef(key=key, stage=index, threshold=threshold, name=label))
    return tuple(found)


def stage_for(count: int, defs: Sequence[AchievementDef]) -> int:
    reached = 0
    for row in sorted(defs, key=lambda item: item.stage):
        if int(count) >= row.threshold:
            reached = row.stage
    return reached


def newly_reached(prev_count: int, new_count: int, defs: Sequence[AchievementDef]) -> tuple[AchievementDef, ...]:
    if new_count <= prev_count:
        return ()
    return tuple(row for row in sorted(defs, key=lambda item: item.stage) if prev_count < row.threshold <= new_count)


def role_for_stage(stage: int, defs: Sequence[AchievementDef]) -> int | None:
    for row in defs:
        if row.stage == stage and row.role_id:
            return int(row.role_id)
    return None


def role_diff(
    have: Iterable[int],
    defs: Sequence[AchievementDef],
    target_stage: int,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    have_ids = set(int(role_id) for role_id in have)
    owned = tuple(int(row.role_id) for row in defs if row.role_id)
    want = role_for_stage(target_stage, defs)
    add = (want,) if want and want not in have_ids else ()
    remove = tuple(role_id for role_id in owned if role_id in have_ids and role_id != want)
    return add, remove


def all_role_ids(defs: Sequence[AchievementDef]) -> tuple[int, ...]:
    return tuple(int(row.role_id) for row in defs if row.role_id)


def parse_achievement_names(raw: str) -> dict[str, tuple[str, ...]] | None:
    lines = [line.strip() for line in str(raw or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    expected = sum(len(KEY_STAGES[key]) for key in KEYS)
    if len(lines) != expected:
        return None
    if any(not line or len(line) > NAME_LIMIT for line in lines):
        return None
    parsed: dict[str, tuple[str, ...]] = {}
    offset = 0
    for key in KEYS:
        count = len(KEY_STAGES[key])
        parsed[key] = tuple(lines[offset : offset + count])
        offset += count
    return parsed


def parse_count_reason(raw: str) -> tuple[int, str] | None:
    first, _, rest = str(raw or "").partition("\n")
    try:
        count = int(first.strip())
    except (TypeError, ValueError):
        return None
    if count < 0:
        return None
    return count, rest.strip()


def next_need(count: int, defs: Sequence[AchievementDef]) -> tuple[int, int, AchievementDef | None]:
    current = stage_for(count, defs)
    nxt = next((row for row in sorted(defs, key=lambda item: item.stage) if row.stage == current + 1), None)
    if nxt is None:
        last = max(defs, key=lambda item: item.stage) if defs else None
        need = 0 if last is None else last.threshold
        return max(0, count), need, None
    return max(0, count), nxt.threshold, nxt
