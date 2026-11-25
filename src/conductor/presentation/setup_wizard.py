"""Карточки мастера /admin → «Каркас»: листы и бюджет разметки."""

from __future__ import annotations

from collections.abc import Sequence

from discord import ui

from conductor.domain.admin import PageControl
from conductor.domain.seed import SeedPlan
from conductor.presentation import copy

__all__ = [
    "LEAF_CHANNELS",
    "LEAF_NOTIFY",
    "LEAF_OVERVIEW",
    "LEAF_ROLES",
    "LEAF_SHOWCASE",
    "SKELETON_LEAVES",
    "action_rows",
    "leaf_nav",
    "plan_question",
    "select_option_counts",
]

LEAF_OVERVIEW = 1
LEAF_CHANNELS = 2
LEAF_ROLES = 3
LEAF_SHOWCASE = 4
LEAF_NOTIFY = 5
SKELETON_LEAVES = 5


def leaf_nav(current: int) -> tuple[PageControl, ...]:
    _ = current
    return tuple(
        PageControl("button", f"skel.leaf:{index}", label)
        for index, label in enumerate(copy.SKELETON_LEAF_LABELS, start=1)
    )


def plan_question(plan: SeedPlan) -> str:
    return copy.skeleton_build_plan(
        create=len(plan.create),
        adopt=len(plan.by_name),
        repair=len(plan.repair),
    )


def action_rows(view: ui.LayoutView) -> list[ui.ActionRow]:
    found: list[ui.ActionRow] = []

    def walk(node: object) -> None:
        if isinstance(node, ui.ActionRow):
            found.append(node)
            return
        for child in getattr(node, "children", None) or ():
            walk(child)

    walk(view)
    return found


def select_option_counts(view: ui.LayoutView) -> Sequence[int]:
    counts: list[int] = []
    for row in action_rows(view):
        for item in getattr(row, "children", ()) or ():
            options = getattr(item, "options", None)
            if options is not None:
                counts.append(len(options))
    return tuple(counts)
