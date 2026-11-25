"""Какие серверы бот имеет право занимать и обслуживать. Без Discord и без БД."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BotMode(StrEnum):
    LIVE = "live"
    TEST = "test"


def parse_bot_mode(value: object) -> BotMode:
    text = str(value or BotMode.LIVE).strip().lower()
    if text in {BotMode.TEST.value, "testing", "dev", "beta"}:
        return BotMode.TEST
    return BotMode.LIVE


def parse_id_csv(value: object) -> tuple[int, ...]:
    """Список snowflake id через запятую или точку с запятой. Пустое — пустой кортеж."""
    if value is None or value == "" or isinstance(value, bool):
        return ()
    if isinstance(value, int):
        return (int(value),) if int(value) > 0 else ()
    if isinstance(value, str):
        parts = [piece.strip() for piece in value.replace(";", ",").split(",")]
        found: list[int] = []
        for part in parts:
            if not part:
                continue
            number = int(part)
            if number > 0:
                found.append(number)
        return tuple(found)
    if isinstance(value, list | tuple | set | frozenset):
        found = []
        for item in value:
            number = int(item)
            if number > 0:
                found.append(number)
        return tuple(found)
    return ()


def resolve_allowed_guilds(
    *,
    explicit: tuple[int, ...] = (),
    test_ids: tuple[int, ...] = (),
    public_id: int | None = None,
    test_id: int | None = None,
    mode: BotMode = BotMode.LIVE,
) -> frozenset[int]:
    """Серверы выбранного режима. Один процесс — один список."""
    if mode is BotMode.TEST:
        if test_ids:
            return frozenset(int(guild_id) for guild_id in test_ids if int(guild_id) > 0)
        if test_id is not None and int(test_id) > 0:
            return frozenset({int(test_id)})
        if explicit:
            return frozenset(int(guild_id) for guild_id in explicit if int(guild_id) > 0)
        return frozenset()
    if explicit:
        return frozenset(int(guild_id) for guild_id in explicit if int(guild_id) > 0)
    if public_id is not None and int(public_id) > 0:
        return frozenset({int(public_id)})
    return frozenset()


@dataclass(frozen=True, slots=True)
class GuildFence:
    guild_ids: frozenset[int]
    mode: BotMode = BotMode.LIVE

    def __post_init__(self) -> None:
        clean = frozenset(int(guild_id) for guild_id in self.guild_ids if int(guild_id) > 0)
        object.__setattr__(self, "guild_ids", clean)

    @property
    def resident_ids(self) -> frozenset[int]:
        return self.guild_ids

    @property
    def active_ids(self) -> frozenset[int]:
        return self.guild_ids

    def may_reside(self, guild_id: int) -> bool:
        return int(guild_id) in self.guild_ids

    def serves(self, guild_id: int) -> bool:
        return int(guild_id) in self.guild_ids

    def sync_scope(self) -> str:
        active = ",".join(str(guild_id) for guild_id in sorted(self.guild_ids))
        return f"{self.mode.value}:{active}"
