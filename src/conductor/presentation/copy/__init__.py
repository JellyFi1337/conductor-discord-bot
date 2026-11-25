"""Каталог пользовательских строк.

Тон вычитывается здесь, а не по cog'ам. Второй язык — второй модуль рядом, не правки команд."""

from __future__ import annotations

from . import (
    admin,
    automod,
    bugs,
    commands,
    common,
    config,
    diagnose,
    errors,
    helptext,
    ladder,
    logs,
    loyalty,
    moderation,
    ops,
    profile,
    recruit,
    reputation,
    roles,
    social,
    stats,
    tickets,
    voice,
    watchers,
)

_MODULES = (
    admin,
    automod,
    bugs,
    commands,
    common,
    config,
    diagnose,
    errors,
    helptext,
    ladder,
    logs,
    loyalty,
    moderation,
    ops,
    profile,
    recruit,
    reputation,
    roles,
    social,
    stats,
    tickets,
    voice,
    watchers,
)


def __getattr__(name: str):
    for module in _MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    names: set[str] = set()
    for module in _MODULES:
        names.update(item for item in dir(module) if not item.startswith("_"))
    return sorted(names)


__all__ = __dir__()
