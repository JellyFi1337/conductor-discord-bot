from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from conductor.domain.guilds import (
    BotMode,
    GuildFence,
    parse_bot_mode,
    parse_id_csv,
    resolve_allowed_guilds,
)
from conductor.domain.privacy import BACKUP_KEEP, CACHE_TTL_SECONDS, RETENTION_DAYS
from conductor.domain.tickets import TICKET_TRANSCRIPT_DAYS
from conductor.domain.traffic import PROFILES, ProfileLimits, RateProfile, apply_overrides

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = (PROJECT_ROOT / "data").resolve()

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
_PLACEHOLDER_TOKENS = frozenset({"your-bot-token", "changeme", "token", "xxx"})
#: Blank values here must not fall back to a default that would be *less* safe.
_KEEP_BLANK = frozenset({"DISCORD_TOKEN", "discord_token"})
_SQLITE_MEMORY = ":memory:"


def sqlite_url_target(url: str) -> str | None:
    """Path or `:memory:` after `sqlite+driver:///`. None if this is not SQLite."""
    if "sqlite" not in url:
        return None
    if "///" not in url:
        return _SQLITE_MEMORY if _SQLITE_MEMORY in url else None
    return url.split("///", 1)[-1]


def is_sqlite_memory(url: str) -> bool:
    raw = sqlite_url_target(url)
    if raw is None:
        return False
    token = raw.strip()
    return token == _SQLITE_MEMORY or token.startswith(f"{_SQLITE_MEMORY}:")


def resolve_sqlite_file(raw: str, data_dir: Path) -> Path | None:
    """Filesystem path for a sqlite URL target. None for `:memory:`."""
    token = raw.strip()
    if token == _SQLITE_MEMORY or token.startswith(f"{_SQLITE_MEMORY}:"):
        return None
    path = Path(token)
    if path.is_absolute():
        return path.resolve()
    parts = [part for part in path.parts if part not in {".", ""}]
    if parts[:1] == ["data"]:
        parts = parts[1:]
    relative = Path(*parts) if parts else Path("conductor.db")
    return (data_dir / relative).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    bot_mode: BotMode = Field(default=BotMode.LIVE, alias="BOT_MODE")
    discord_token: str = Field(default="", alias="DISCORD_TOKEN")
    guild_ids: tuple[int, ...] = Field(default=(), alias="GUILD_IDS")
    public_guild_id: int | None = Field(default=None, alias="PUBLIC_GUILD_ID")
    test_discord_token: str = Field(default="", alias="TEST_DISCORD_TOKEN")
    test_guild_ids: tuple[int, ...] = Field(default=(), alias="TEST_GUILD_IDS")
    test_guild_id: int | None = Field(default=None, alias="TEST_GUILD_ID")
    owner_user_ids: tuple[int, ...] = Field(default=(), alias="OWNER_USER_IDS")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/conductor.db",
        alias="DATABASE_URL",
    )
    data_dir: Path = Field(default=DEFAULT_DATA_DIR, alias="DATA_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    message_cache_size: int = Field(default=20_000, ge=0, le=1_000_000, alias="MESSAGE_CACHE_SIZE")
    message_cache_ttl: int = Field(default=CACHE_TTL_SECONDS, ge=1, le=604_800, alias="MESSAGE_CACHE_TTL")
    data_retention_days: int = Field(default=RETENTION_DAYS, ge=0, le=3_650, alias="DATA_RETENTION_DAYS")
    ticket_transcript_days: int = Field(default=TICKET_TRANSCRIPT_DAYS, ge=0, le=3_650, alias="TICKET_TRANSCRIPT_DAYS")
    sqlite_backup_keep: int = Field(default=BACKUP_KEEP, ge=0, le=90, alias="SQLITE_BACKUP_KEEP")
    voice_rooms_per_member: int = Field(default=1, ge=1, le=20, alias="VOICE_ROOMS_PER_MEMBER")
    tickets_per_category: int = Field(default=1, ge=1, le=20, alias="TICKETS_PER_CATEGORY")
    interaction_limit_per_minute: int = Field(default=50, ge=1, le=10_000, alias="INTERACTION_LIMIT_PER_MINUTE")
    rate_profile: RateProfile = Field(default=RateProfile.EDGE, alias="RATE_PROFILE")
    global_rps_target: int | None = Field(default=None, ge=1, le=50, alias="GLOBAL_RPS_TARGET")
    outbound_concurrency: int | None = Field(default=None, ge=1, le=50, alias="OUTBOUND_CONCURRENCY")
    max_ratelimit_wait: float | None = Field(default=None, ge=1, le=600, alias="MAX_RATELIMIT_WAIT")
    invalid_request_stop: int | None = Field(default=None, ge=1, le=10_000, alias="INVALID_REQUEST_STOP")
    force_sync: bool = Field(default=False, alias="FORCE_SYNC")
    auto_migrate: bool | None = Field(default=None, alias="AUTO_MIGRATE")

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_keys(cls, data: Any) -> Any:
        """Пустой ключ = «не задано». `DEV_GUILD_ID` читается как запасной `TEST_GUILD_ID`."""
        if not isinstance(data, dict):
            return data
        test = data.get("TEST_GUILD_ID", data.get("test_guild_id"))
        legacy = data.get("DEV_GUILD_ID", data.get("dev_guild_id"))
        test_blank = test is None or (isinstance(test, str) and not test.strip())
        if test_blank and legacy not in {None, ""}:
            data["TEST_GUILD_ID"] = legacy
        return {
            key: value
            for key, value in data.items()
            if key in _KEEP_BLANK or not (isinstance(value, str) and not value.strip())
        }

    @field_validator("data_dir", mode="before")
    @classmethod
    def _absolute_data_dir(cls, value: object) -> Path:
        if value is None or value == "":
            return DEFAULT_DATA_DIR
        folder = Path(value).expanduser()
        if not folder.is_absolute():
            folder = PROJECT_ROOT / folder
        return folder.resolve()

    @model_validator(mode="after")
    def _resolve_sqlite_url(self) -> Settings:
        if "sqlite" not in self.database_url:
            return self
        if is_sqlite_memory(self.database_url):
            self.database_url = f"sqlite+aiosqlite:///{_SQLITE_MEMORY}"
            return self
        raw = sqlite_url_target(self.database_url)
        if raw is None:
            return self
        resolved = resolve_sqlite_file(raw, self.data_dir)
        if resolved is None:
            self.database_url = f"sqlite+aiosqlite:///{_SQLITE_MEMORY}"
            return self
        self.database_url = f"sqlite+aiosqlite:///{resolved.as_posix()}"
        return self

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        return level if level in _LOG_LEVELS else "INFO"

    @field_validator("bot_mode", mode="before")
    @classmethod
    def _known_bot_mode(cls, value: object) -> BotMode:
        return parse_bot_mode(value)

    @field_validator("rate_profile", mode="before")
    @classmethod
    def _known_rate_profile(cls, value: object) -> RateProfile:
        try:
            return RateProfile(str(value or "").strip().lower())
        except ValueError:
            return RateProfile.EDGE

    @field_validator("guild_ids", "test_guild_ids", "owner_user_ids", mode="before")
    @classmethod
    def _id_csv(cls, value: object) -> tuple[int, ...]:
        return parse_id_csv(value)

    @model_validator(mode="after")
    def _select_process_token(self) -> Settings:
        """Оба токена лежат в одном .env. BOT_MODE=test|beta берёт TEST_DISCORD_TOKEN, если он задан."""
        if self.bot_mode is BotMode.TEST:
            selected = self.test_discord_token.strip()
            if selected:
                self.discord_token = selected
        return self

    @property
    def log_level_number(self) -> int:
        return getattr(logging, self.log_level, logging.INFO)

    @property
    def token_looks_real(self) -> bool:
        token = self.discord_token.strip()
        return bool(token) and token.lower() not in _PLACEHOLDER_TOKENS

    @property
    def sqlite_path(self) -> Path | None:
        raw = sqlite_url_target(self.database_url)
        if raw is None:
            return None
        return resolve_sqlite_file(raw, self.data_dir)

    @property
    def fence(self) -> GuildFence | None:
        ids = resolve_allowed_guilds(
            explicit=self.guild_ids,
            test_ids=self.test_guild_ids,
            public_id=self.public_guild_id,
            test_id=self.test_guild_id,
            mode=self.bot_mode,
        )
        if not ids:
            return None
        return GuildFence(guild_ids=ids, mode=self.bot_mode)

    @property
    def mode_label(self) -> str:
        fence = self.fence
        if fence is None:
            return "серверы не заданы"
        listed = ", ".join(str(guild_id) for guild_id in sorted(fence.guild_ids))
        if fence.mode is BotMode.TEST:
            return f"тест · {listed}"
        return f"live · {listed}"

    @property
    def auto_migrate_enabled(self) -> bool:
        if self.auto_migrate is not None:
            return self.auto_migrate
        return "sqlite" in self.database_url

    @property
    def traffic_limits(self) -> ProfileLimits:
        return apply_overrides(
            PROFILES[self.rate_profile],
            global_rps_target=self.global_rps_target,
            outbound_concurrency=self.outbound_concurrency,
            max_ratelimit_wait=self.max_ratelimit_wait,
            invalid_request_stop=self.invalid_request_stop,
        )


def masked_url(url: str) -> str:
    """URL базы для лога: пароль между «:» и «@» заменён звёздочками."""
    scheme, sep, rest = str(url or "").partition("://")
    if not sep:
        return str(url or "")
    creds, at, host = rest.rpartition("@")
    if not at or ":" not in creds:
        return str(url or "")
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
