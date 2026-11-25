"""Значимые объекты, общие для нескольких слоёв.

Живут в домене, а не в services/, потому что их читает presentation/: карточка
события должна знать форму данных, но не должна зависеть от того, кто их собрал.
Чистые данные, без Discord и без БД.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class UsedInvite:
    """Приглашение, по которому участник попал на сервер."""

    code: str
    inviter_id: int | None
    inviter_name: str
    uses: int
    max_uses: int | None
    channel_id: int | None
    vanity: bool = False


def log_channel_ids(*channel_ids: int | None) -> frozenset[int]:
    """Каналы логов гильдии: пустые значения отбрасываются."""

    return frozenset(int(channel_id) for channel_id in channel_ids if channel_id)


@dataclass(slots=True, frozen=True)
class CachedMessage:
    """Снимок сообщения на момент получения: нужен, когда оригинал уже удалён."""

    id: int
    guild_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: datetime
    attachments: tuple[str, ...]
    jump_url: str = ""
    stickers: tuple[str, ...] = ()
    reply_id: int | None = None
    is_bot: bool = False
