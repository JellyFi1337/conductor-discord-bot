from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

from conductor.domain.mutations import MutationsLockedError

ROLE_BROKER_ACTIVE: ContextVar[bool] = ContextVar("conductor_role_broker", default=False)

log = structlog.get_logger("conductor.mutations")

_PATCH_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (("discord.member.Member", ("add_roles", "remove_roles")),)


def _import_target(path: str) -> type[Any] | None:
    module_name, class_name = path.rsplit(".", 1)
    try:
        module = __import__(module_name, fromlist=[class_name])
        return getattr(module, class_name)
    except (ImportError, AttributeError):
        return None


@contextmanager
def role_broker_allowed() -> Iterator[None]:
    """Разрешает add_roles/remove_roles только коду брокера, пока открыт контекст."""
    token = ROLE_BROKER_ACTIVE.set(True)
    try:
        yield
    finally:
        ROLE_BROKER_ACTIVE.reset(token)


def make_role_broker_gate(original: Any, action: str) -> Callable[..., Awaitable[Any]]:
    async def gated(*args: Any, **kwargs: Any) -> Any:
        if ROLE_BROKER_ACTIVE.get():
            return await original(*args, **kwargs)
        raise MutationsLockedError(action)

    gated.__name__ = f"broker_{action}"
    gated.__qualname__ = f"broker_{action}"
    return gated


class DiscordMutationLock:
    """Патч add_roles/remove_roles: только брокер ролей."""

    def __init__(self) -> None:
        self._originals: list[tuple[type[Any], str, Any]] = []
        self.installed = False

    def install(self, _guard: object | None = None) -> int:
        if self.installed:
            return len(self._originals)
        for dotted, methods in _PATCH_TARGETS:
            cls = _import_target(dotted)
            if cls is None:
                continue
            for name in methods:
                original = getattr(cls, name, None)
                if original is None:
                    continue
                self._originals.append((cls, name, original))
                setattr(cls, name, make_role_broker_gate(original, f"{cls.__name__}.{name}"))
        self.installed = True
        log.info("role_broker_gate_installed", methods=len(self._originals))
        return len(self._originals)

    def _original(self, cls: type[Any], name: str) -> Any:
        for stored_cls, stored_name, original in self._originals:
            if stored_name == name and stored_cls.__name__ == cls.__name__:
                return original
        return getattr(cls, name)

    def uninstall(self) -> None:
        for cls, name, original in self._originals:
            setattr(cls, name, original)
        self._originals.clear()
        self.installed = False
