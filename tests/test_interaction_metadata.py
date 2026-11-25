"""К9: Message.interaction не читается, только interaction_metadata."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
_MESSAGE_NAMES = frozenset({"message", "msg", "before", "after"})


def test_message_interaction_is_not_read() -> None:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "interaction"
                and isinstance(node.value, ast.Name)
                and node.value.id in _MESSAGE_NAMES
            ):
                hits.append(f"{rel}:{node.lineno}")
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id != "getattr" or len(node.args) < 2:
                continue
            key = node.args[1]
            if isinstance(key, ast.Constant) and key.value == "interaction":
                hits.append(f"{rel}:{node.lineno}")
    assert not hits, "чтение Message.interaction:\n  " + "\n  ".join(hits)
