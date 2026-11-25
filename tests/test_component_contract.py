"""Контракт К4: callback только у PersistentButton/PersistentSelect, присваивания — acked/instant."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import conductor.presentation
from conductor.presentation.persistent import PersistentButton, PersistentSelect, load_persistent_ui

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
_WRAPS = frozenset({"acked", "instant"})


def _import_presentation() -> None:
    load_persistent_ui()
    for module in pkgutil.walk_packages(conductor.presentation.__path__, conductor.presentation.__name__ + "."):
        importlib.import_module(module.name)


def _walk_subclasses(base: type) -> list[type]:
    found: list[type] = []

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            found.append(sub)
            walk(sub)

    walk(base)
    return found


def test_persistent_subclasses_do_not_define_callback() -> None:
    _import_presentation()
    bases = (PersistentButton, PersistentSelect)
    hits: list[str] = []
    for cls in (*_walk_subclasses(PersistentButton), *_walk_subclasses(PersistentSelect)):
        if not cls.__module__.startswith("conductor."):
            continue
        for ancestor in cls.__mro__:
            if ancestor in bases or not ancestor.__module__.startswith("conductor."):
                continue
            if "callback" in ancestor.__dict__:
                hits.append(f"{ancestor.__module__}.{ancestor.__qualname__}")
    assert not hits, "callback вне базовых PersistentButton/PersistentSelect:\n  " + "\n  ".join(hits)


def _wrapped_callback(value: ast.AST) -> bool:
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    return isinstance(func, ast.Name) and func.id in _WRAPS


def test_assigned_callbacks_use_acked_or_instant() -> None:
    hits: list[str] = []
    root = SRC / "presentation"
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute) or target.attr != "callback":
                    continue
                if _wrapped_callback(node.value):
                    continue
                rel = path.relative_to(SRC).as_posix()
                hits.append(f"{rel}:{node.lineno}")
    assert not hits, "присваивание callback без acked/instant:\n  " + "\n  ".join(hits)
