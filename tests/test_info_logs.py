"""Содержимое сообщений участников не должно попадать в log.info — это инвариант, не договорённость."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
_OWNERS = frozenset({"message", "msg", "cached", "payload", "before", "after", "quote"})
_CONTENT_KW = frozenset({"content", "message_content", "snippet"})


def _looks_like_message(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _OWNERS
    if isinstance(node, ast.Attribute):
        return node.attr in _OWNERS
    return False


def _is_content_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in _CONTENT_KW


def _carries_member_content(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "content" and _looks_like_message(node.value):
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and _looks_like_message(node.args[0])
        and _is_content_str(node.args[1])
    ):
        return True
    return any(_carries_member_content(child) for child in ast.iter_child_nodes(node))


def _is_info_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "info"


def test_info_logs_do_not_include_member_message_content() -> None:
    problems: list[str] = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(SRC)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_info_call(node):
                continue
            for arg in node.args:
                if _carries_member_content(arg):
                    problems.append(f"{rel}:{node.lineno}: log.info передаёт содержимое сообщения")
            for kw in node.keywords:
                if kw.arg in _CONTENT_KW:
                    problems.append(f"{rel}:{node.lineno}: log.info(..., {kw.arg}=...) — поле содержимого сообщения")
                elif kw.value is not None and _carries_member_content(kw.value):
                    problems.append(f"{rel}:{node.lineno}: log.info(..., {kw.arg}=...) тянет message.content")
    assert not problems, "INFO не должен содержать текст сообщений участников:\n  " + "\n  ".join(problems)
