"""Отметка живости процесса для healthcheck контейнера.

Бот не слушает порт, поэтому Docker не может проверить его обычной пробой.
Планировщик раз в 30 с трогает файл в каталоге данных; healthcheck смотрит на
возраст файла. Файл стареет, если умер цикл событий или встал планировщик, —
именно то, чего не видно по «процесс запущен».
"""

from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger("conductor.heartbeat")

PERIOD_SECONDS = 30.0
FILE_NAME = ".heartbeat"
#: Порог для healthcheck в docker-compose.yml. Менять оба места вместе.
STALE_SECONDS = 120

__all__ = ["FILE_NAME", "PERIOD_SECONDS", "STALE_SECONDS", "heartbeat_path", "touch"]


def heartbeat_path(settings: object | None = None) -> Path:
    from conductor.config import DEFAULT_DATA_DIR

    folder = getattr(settings, "data_dir", None) or DEFAULT_DATA_DIR
    return Path(folder) / FILE_NAME


def touch(path: Path) -> bool:
    """Обновить mtime. False — каталог недоступен: это не повод падать."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    except OSError as error:
        log.warning("heartbeat_failed", path=str(target), error=str(error))
        return False
    return True
