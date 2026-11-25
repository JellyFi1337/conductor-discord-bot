"""Отложенное переименование канала: лимит Discord 2 раза за 10 минут."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

WINDOW_SECONDS = 600
RENAME_LIMIT = 2


@dataclass(frozen=True, slots=True)
class Deferred:
    until: datetime
