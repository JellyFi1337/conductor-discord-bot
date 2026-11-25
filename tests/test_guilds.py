from conductor.bot import event_guild_id
from conductor.config import Settings
from conductor.domain.access import ACTION_DEFAULTS, CONFIG_MANAGE, Rank, authorize
from conductor.domain.guilds import (
    BotMode,
    GuildFence,
    parse_bot_mode,
    parse_id_csv,
    resolve_allowed_guilds,
)
from conductor.services.runtime import guild_is_active


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_parse_bot_mode() -> None:
    assert parse_bot_mode("live") is BotMode.LIVE
    assert parse_bot_mode("TEST") is BotMode.TEST
    assert parse_bot_mode("dev") is BotMode.TEST
    assert parse_bot_mode("beta") is BotMode.TEST
    assert parse_bot_mode("") is BotMode.LIVE


def test_parse_id_csv() -> None:
    assert parse_id_csv("") == ()
    assert parse_id_csv(None) == ()
    assert parse_id_csv("11, 22;33") == (11, 22, 33)
    assert parse_id_csv(7) == (7,)
    assert parse_id_csv(True) == ()
    assert parse_id_csv("0, -1, 5") == (5,)


def test_fence_serves_only_listed_and_rejects_strangers() -> None:
    fence = GuildFence(guild_ids=frozenset({1, 2}), mode=BotMode.LIVE)
    assert fence.resident_ids == frozenset({1, 2})
    assert fence.active_ids == frozenset({1, 2})
    assert fence.may_reside(1) is True
    assert fence.may_reside(9) is False
    assert fence.serves(1) is True
    assert fence.serves(2) is True
    assert fence.serves(9) is False
    assert fence.sync_scope() == "live:1,2"


def test_fence_test_mode_leaves_unlisted_public() -> None:
    fence = GuildFence(guild_ids=frozenset({2}), mode=BotMode.TEST)
    assert fence.may_reside(1) is False
    assert fence.serves(1) is False
    assert fence.serves(2) is True
    assert fence.sync_scope() == "test:2"


def test_resolve_allowed_guilds_prefers_explicit_list() -> None:
    ids = resolve_allowed_guilds(
        explicit=(100, 200),
        test_ids=(9,),
        public_id=11,
        test_id=22,
        mode=BotMode.TEST,
    )
    assert ids == frozenset({9})
    fallback = resolve_allowed_guilds(
        explicit=(100, 200),
        public_id=11,
        test_id=22,
        mode=BotMode.TEST,
    )
    assert fallback == frozenset({22})


def test_resolve_allowed_guilds_test_mode_drops_public() -> None:
    ids = resolve_allowed_guilds(public_id=11, test_id=22, mode=BotMode.TEST)
    assert ids == frozenset({22})


def test_settings_read_guild_fence_and_legacy_dev_id() -> None:
    settings = _settings(
        DISCORD_TOKEN="t",
        PUBLIC_GUILD_ID="11",
        DEV_GUILD_ID="22",
        BOT_MODE="test",
        OWNER_USER_IDS="7, 8",
    )
    assert settings.public_guild_id == 11
    assert settings.test_guild_id == 22
    assert settings.bot_mode is BotMode.TEST
    assert settings.owner_user_ids == (7, 8)
    assert settings.fence is not None
    assert settings.fence.serves(11) is False
    assert settings.fence.may_reside(11) is False
    assert settings.fence.serves(22) is True
    assert settings.mode_label == "тест · 22"


def test_settings_guild_ids_override_legacy_keys() -> None:
    settings = _settings(
        DISCORD_TOKEN="t",
        GUILD_IDS="100, 200",
        PUBLIC_GUILD_ID="11",
        TEST_GUILD_ID="22",
        BOT_MODE="live",
    )
    assert settings.guild_ids == (100, 200)
    assert settings.fence is not None
    assert settings.fence.guild_ids == frozenset({100, 200})
    assert settings.mode_label == "live · 100, 200"


def test_settings_live_accepts_public_only() -> None:
    settings = _settings(DISCORD_TOKEN="t", PUBLIC_GUILD_ID="11", BOT_MODE="live")
    assert settings.fence is not None
    assert settings.fence.guild_ids == frozenset({11})
    assert settings.fence.serves(11) is True


def test_settings_live_uses_only_public_without_guild_ids() -> None:
    settings = _settings(
        DISCORD_TOKEN="t",
        PUBLIC_GUILD_ID="11",
        TEST_GUILD_ID="22",
        BOT_MODE="live",
    )
    assert settings.fence is not None
    assert settings.fence.guild_ids == frozenset({11})
    assert settings.mode_label == "live · 11"


def test_settings_test_mode_uses_test_token_and_guilds() -> None:
    settings = _settings(
        DISCORD_TOKEN="live-token-value",
        TEST_DISCORD_TOKEN="test-token-value",
        GUILD_IDS="11",
        TEST_GUILD_IDS="22, 33",
        BOT_MODE="beta",
    )
    assert settings.bot_mode is BotMode.TEST
    assert settings.discord_token == "test-token-value"
    assert settings.fence is not None
    assert settings.fence.guild_ids == frozenset({22, 33})
    assert settings.token_looks_real is True


def test_settings_live_mode_keeps_live_token_when_both_are_set() -> None:
    settings = _settings(
        DISCORD_TOKEN="live-token-value",
        TEST_DISCORD_TOKEN="test-token-value",
        GUILD_IDS="11",
        TEST_GUILD_IDS="22",
        BOT_MODE="live",
    )
    assert settings.bot_mode is BotMode.LIVE
    assert settings.discord_token == "live-token-value"
    assert settings.fence is not None
    assert settings.fence.guild_ids == frozenset({11})


def test_blank_guild_ids_stay_unset() -> None:
    settings = _settings(DISCORD_TOKEN="t", PUBLIC_GUILD_ID="", TEST_GUILD_ID="", GUILD_IDS="")
    assert settings.public_guild_id is None
    assert settings.test_guild_id is None
    assert settings.guild_ids == ()
    assert settings.fence is None


def test_developer_bypasses_ranks() -> None:
    decision = authorize(
        rank=Rank.NONE,
        key=CONFIG_MANAGE,
        allowed=ACTION_DEFAULTS[CONFIG_MANAGE],
        is_administrator=False,
        special_locked=True,
        is_developer=True,
    )
    assert decision.allowed is True
    assert decision.developer is True
    assert decision.rank is Rank.SPECIAL


def test_guild_is_active_respects_bot_serves() -> None:
    assert guild_is_active(object(), 1) is True
    bot = type("Bot", (), {"serves": staticmethod(lambda guild_id: guild_id == 2)})()
    assert guild_is_active(bot, 1) is False
    assert guild_is_active(bot, 2) is True


def test_event_guild_id_reads_nested_guild() -> None:
    class Guild:
        id = 15

    class Message:
        guild = Guild()
        guild_id = None

    assert event_guild_id(Message()) == 15
    assert event_guild_id(type("Raw", (), {"guild_id": 33})()) == 33
    assert event_guild_id(None, "x") is None
