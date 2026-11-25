from pathlib import Path

from conductor.config import DEFAULT_DATA_DIR, Settings
from conductor.domain.traffic import RateProfile


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_blank_optional_keys_are_read_as_unset() -> None:
    """`.env.example` ships blank guild ids; that used to crash the bot on first start."""
    settings = _settings(
        DISCORD_TOKEN="real-token",
        PUBLIC_GUILD_ID="",
        TEST_GUILD_ID="",
        GUILD_IDS="",
        LOG_LEVEL="",
        MESSAGE_CACHE_SIZE="",
        DATABASE_URL="",
    )
    assert settings.public_guild_id is None
    assert settings.test_guild_id is None
    assert settings.log_level == "INFO"
    assert settings.message_cache_size == 20_000
    assert settings.database_url.startswith("sqlite+aiosqlite")


def test_blank_flags_keep_the_safe_default() -> None:
    settings = _settings(DISCORD_TOKEN="real-token", FORCE_SYNC="")
    assert settings.force_sync is False
    assert settings.bot_mode.value == "live"


def test_force_sync_is_off_unless_explicitly_enabled() -> None:
    assert _settings(DISCORD_TOKEN="real-token").force_sync is False
    assert _settings(DISCORD_TOKEN="real-token", FORCE_SYNC="true").force_sync is True


def test_placeholder_token_is_not_accepted() -> None:
    assert _settings(DISCORD_TOKEN="your-bot-token").token_looks_real is False
    assert _settings(DISCORD_TOKEN="  ").token_looks_real is False
    assert _settings(DISCORD_TOKEN="MTIz.abc").token_looks_real is True


def test_unknown_log_level_falls_back_instead_of_raising() -> None:
    assert _settings(DISCORD_TOKEN="t", LOG_LEVEL="loud").log_level == "INFO"
    assert _settings(DISCORD_TOKEN="t", LOG_LEVEL="debug").log_level == "DEBUG"


def test_sqlite_path_is_exposed_for_preflight() -> None:
    settings = _settings(DISCORD_TOKEN="t", DATABASE_URL="sqlite+aiosqlite:///./data/conductor.db")
    assert settings.sqlite_path is not None
    assert settings.sqlite_path.name == "conductor.db"
    assert settings.sqlite_path.is_absolute()
    assert _settings(DISCORD_TOKEN="t", DATABASE_URL="postgresql+asyncpg://x/y").sqlite_path is None


def test_data_dir_defaults_to_project_data(monkeypatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    settings = _settings(DISCORD_TOKEN="t")
    assert settings.data_dir == DEFAULT_DATA_DIR
    assert settings.data_dir.is_absolute()
    assert settings.data_dir.name == "data"
    assert settings.sqlite_path == settings.data_dir / "conductor.db"


def test_data_dir_env_wins(tmp_path: Path) -> None:
    folder = tmp_path / "custom"
    settings = _settings(DISCORD_TOKEN="t", DATA_DIR=str(folder))
    assert settings.data_dir == folder.resolve()
    assert settings.sqlite_path == settings.data_dir / "conductor.db"


def test_sqlite_memory_url_is_not_a_file(tmp_path: Path) -> None:
    settings = _settings(
        DISCORD_TOKEN="t",
        DATA_DIR=str(tmp_path),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    assert settings.sqlite_path is None
    assert settings.database_url == "sqlite+aiosqlite:///:memory:"
    assert not (tmp_path / ":memory:").exists()


def test_sqlite_relative_url_keeps_nested_directories(tmp_path: Path) -> None:
    settings = _settings(
        DISCORD_TOKEN="t",
        DATA_DIR=str(tmp_path),
        DATABASE_URL="sqlite+aiosqlite:///custom/nested/app.db",
    )
    assert settings.sqlite_path == (tmp_path / "custom" / "nested" / "app.db").resolve()
    assert "custom/nested/app.db" in settings.database_url


def test_sqlite_data_prefix_is_not_duplicated(tmp_path: Path) -> None:
    settings = _settings(
        DISCORD_TOKEN="t",
        DATA_DIR=str(tmp_path),
        DATABASE_URL="sqlite+aiosqlite:///./data/custom/nested.db",
    )
    assert settings.sqlite_path == (tmp_path / "custom" / "nested.db").resolve()


def test_sqlite_absolute_url_is_not_relocated(tmp_path: Path) -> None:
    dest = tmp_path / "elsewhere" / "app.db"
    settings = _settings(
        DISCORD_TOKEN="t",
        DATA_DIR=str(tmp_path / "data"),
        DATABASE_URL=f"sqlite+aiosqlite:///{dest.as_posix()}",
    )
    assert settings.sqlite_path == dest.resolve()
    assert settings.sqlite_path != (tmp_path / "data" / "app.db").resolve()


def test_auto_migrate_defaults_to_sqlite_yes_postgres_no() -> None:
    sqlite = _settings(DISCORD_TOKEN="t")
    assert sqlite.auto_migrate is None
    assert sqlite.auto_migrate_enabled is True
    postgres = _settings(DISCORD_TOKEN="t", DATABASE_URL="postgresql+asyncpg://x/y")
    assert postgres.auto_migrate_enabled is False


def test_auto_migrate_blank_uses_the_engine_default() -> None:
    settings = _settings(DISCORD_TOKEN="t", AUTO_MIGRATE="")
    assert settings.auto_migrate is None
    assert settings.auto_migrate_enabled is True


def test_auto_migrate_explicit_overrides_the_engine() -> None:
    assert _settings(DISCORD_TOKEN="t", AUTO_MIGRATE="false").auto_migrate_enabled is False
    assert (
        _settings(
            DISCORD_TOKEN="t",
            DATABASE_URL="postgresql+asyncpg://x/y",
            AUTO_MIGRATE="true",
        ).auto_migrate_enabled
        is True
    )


def test_capacity_defaults_match_the_plan() -> None:
    settings = _settings(DISCORD_TOKEN="t")
    assert settings.voice_rooms_per_member == 1
    assert settings.tickets_per_category == 1
    assert settings.interaction_limit_per_minute == 50
    assert settings.message_cache_ttl == 3600
    assert settings.data_retention_days == 90
    assert settings.sqlite_backup_keep == 7


def test_blank_capacity_keys_keep_defaults() -> None:
    settings = _settings(
        DISCORD_TOKEN="t",
        VOICE_ROOMS_PER_MEMBER="",
        TICKETS_PER_CATEGORY="",
        INTERACTION_LIMIT_PER_MINUTE="",
        MESSAGE_CACHE_TTL="",
        DATA_RETENTION_DAYS="",
        SQLITE_BACKUP_KEEP="",
    )
    assert settings.voice_rooms_per_member == 1
    assert settings.tickets_per_category == 1
    assert settings.interaction_limit_per_minute == 50
    assert settings.message_cache_ttl == 3600
    assert settings.data_retention_days == 90
    assert settings.sqlite_backup_keep == 7


def test_bot_mode_defaults_to_live() -> None:
    settings = _settings(DISCORD_TOKEN="t")
    assert settings.bot_mode.value == "live"
    parsed = _settings(DISCORD_TOKEN="t", BOT_MODE="test")
    assert parsed.bot_mode.value == "test"
    blank = _settings(DISCORD_TOKEN="t", BOT_MODE="")
    assert blank.bot_mode.value == "live"


def test_preflight_requires_a_guild_list(capsys) -> None:
    from conductor.__main__ import _preflight

    settings = _settings(DISCORD_TOKEN="real-token-value")
    try:
        _preflight(settings)
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("preflight must stop without a guild list")
    assert "GUILD_IDS" in capsys.readouterr().err


def test_preflight_accepts_guild_ids_only() -> None:
    from conductor.__main__ import _preflight

    settings = _settings(DISCORD_TOKEN="real-token-value", GUILD_IDS="11")
    _preflight(settings)
    assert settings.fence is not None
    assert settings.fence.guild_ids == frozenset({11})


def test_masked_url_hides_password() -> None:
    from conductor.config import masked_url

    assert masked_url("postgresql+asyncpg://conductor:s3cr3t@postgres:5432/conductor") == (
        "postgresql+asyncpg://conductor:***@postgres:5432/conductor"
    )
    assert "p@ss" not in masked_url("postgresql+asyncpg://u:p@ss@host:5432/db")
    assert masked_url("sqlite+aiosqlite:///C:/data/conductor.db") == "sqlite+aiosqlite:///C:/data/conductor.db"
    assert masked_url("") == ""


def test_preflight_log_never_contains_password(capsys: object) -> None:
    import inspect

    import conductor.__main__ as entry

    source = inspect.getsource(entry._preflight)
    assert "database=settings.database_url" not in source
    assert "masked_url(settings.database_url)" in source


def test_database_hint_names_the_cause() -> None:
    from conductor.__main__ import _database_hint

    class InvalidPasswordError(Exception):
        pass

    class InvalidCatalogNameError(Exception):
        pass

    wrapped = RuntimeError("outer")
    wrapped.__cause__ = InvalidPasswordError("auth")
    assert "POSTGRES_PASSWORD" in (_database_hint(wrapped) or "")
    assert "POSTGRES_DB" in (_database_hint(InvalidCatalogNameError()) or "")
    assert "docker compose ps" in (_database_hint(ConnectionRefusedError()) or "")
    assert _database_hint(ValueError("unrelated")) is None


def test_settings_aliases_are_in_env_example() -> None:
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    missing = [field.alias for field in Settings.model_fields.values() if field.alias and field.alias not in text]
    assert missing == []


def test_rate_profile_defaults_to_edge() -> None:
    settings = _settings(DISCORD_TOKEN="t")
    assert settings.rate_profile is RateProfile.EDGE
    assert settings.global_rps_target is None
    assert settings.traffic_limits.global_rps_target == 48
    assert settings.traffic_limits.outbound_concurrency == 24
    assert settings.traffic_limits.max_ratelimit_wait == 600.0
    assert settings.traffic_limits.invalid_stop == 4500
    assert settings.traffic_limits.click_member_limit == 12
    assert settings.traffic_limits.click_guild_limit == 1200
    assert settings.traffic_limits.role_coalesce == 3.0


def test_rate_profile_overrides_and_blank_keep_profile() -> None:
    blank = _settings(
        DISCORD_TOKEN="t",
        RATE_PROFILE="",
        GLOBAL_RPS_TARGET="",
        OUTBOUND_CONCURRENCY="",
        MAX_RATELIMIT_WAIT="",
        INVALID_REQUEST_STOP="",
    )
    assert blank.rate_profile is RateProfile.EDGE
    assert blank.traffic_limits.global_rps_target == 48
    safe = _settings(DISCORD_TOKEN="t", RATE_PROFILE="safe", GLOBAL_RPS_TARGET="30")
    assert safe.rate_profile is RateProfile.SAFE
    assert safe.traffic_limits.global_rps_target == 30
    assert safe.traffic_limits.max_ratelimit_wait == 30.0
    unknown = _settings(DISCORD_TOKEN="t", RATE_PROFILE="turbo")
    assert unknown.rate_profile is RateProfile.EDGE
