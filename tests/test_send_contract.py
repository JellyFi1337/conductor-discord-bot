"""Контракты К1 и К2: ui.File и view= живут только в layout/motion."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
LAYOUT = Path("presentation") / "layout.py"
MOTION = Path("presentation") / "motion.py"
_SEND_ATTRS = frozenset({"send", "edit", "send_message", "edit_message", "edit_original_response"})


def test_ui_file_only_in_layout() -> None:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC)
        if rel == LAYOUT:
            continue
        text = path.read_text(encoding="utf-8")
        if "ui.File(" in text or "discord.ui.File(" in text:
            hits.append(str(rel).replace("\\", "/"))
    assert not hits, "ui.File вне layout.py:\n  " + "\n  ".join(hits)


def test_view_keyword_only_in_layout_and_motion() -> None:
    allowed = {LAYOUT, MOTION}
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _SEND_ATTRS:
                continue
            for keyword in node.keywords:
                if keyword.arg != "view":
                    continue
                if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                    continue
                if rel not in allowed:
                    hits.append(f"{rel.as_posix()}:{node.lineno}")
    assert not hits, "view= вне layout.py/motion.py:\n  " + "\n  ".join(hits)
