"""Проверка Discord 50035: каждый attachment:// в карточке должен быть в files=/attachments=."""

from __future__ import annotations

from typing import Any

from discord import ui


def assert_attachments_resolved(kwargs: dict[str, Any]) -> None:
    view = kwargs.get("view")
    walk = getattr(view, "walk_children", None)
    if not callable(walk):
        return
    needed: list[str] = []
    for item in walk():
        if not isinstance(item, ui.File):
            continue
        url = getattr(item, "url", None) or getattr(getattr(item, "media", None), "url", None)
        if isinstance(url, str) and url.startswith("attachment://"):
            needed.append(url.removeprefix("attachment://"))
    if not needed:
        return
    attached = list(kwargs.get("files") or ()) + list(kwargs.get("attachments") or ())
    names = {str(getattr(item, "filename", "") or "") for item in attached}
    missing = [name for name in needed if name not in names]
    assert not missing, f"вложение не приложено: {missing}; есть {sorted(names)}"
