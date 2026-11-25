from conductor.domain import discord_limits, rooms, traffic
from conductor.presentation import layout


def test_registry_values_match_discord_platform() -> None:
    assert discord_limits.MENTIONS_PER_MESSAGE == 100
    assert discord_limits.TEXT_DISPLAY_TOTAL == 4000
    assert discord_limits.COMPONENTS_PER_MESSAGE == 40
    assert discord_limits.ACTION_ROW_BUTTONS == 5
    assert discord_limits.SELECT_OPTIONS == 25
    assert discord_limits.MODAL_FIELDS == 5
    assert discord_limits.BUTTON_LABEL == 80
    assert discord_limits.CHANNEL_NAME == 100
    assert discord_limits.CATEGORY_CHILDREN == 50
    assert discord_limits.GUILD_CHANNELS == 500
    assert discord_limits.VOICE_USER_LIMIT_MAX == 99
    assert discord_limits.INTERACTION_ACK_SECONDS == 3.0
    assert discord_limits.INTERACTION_TOKEN_SECONDS == 15 * 60
    assert discord_limits.UPLOAD_BYTES_DEFAULT == 10 * 1024 * 1024
    assert discord_limits.AUDIT_REASON == 512
    assert discord_limits.TIMEOUT_MAX_DAYS == 28
    assert discord_limits.BULK_DELETE_MAX == 100
    assert discord_limits.BULK_DELETE_AGE_DAYS == 14
    assert discord_limits.HISTORY_PAGE == 100


def test_legacy_aliases_point_to_registry() -> None:
    assert layout.TEXT_LIMIT is discord_limits.TEXT_DISPLAY_TOTAL
    assert layout.COMPONENT_LIMIT is discord_limits.COMPONENTS_PER_MESSAGE
    assert layout.BUTTON_LABEL_LIMIT is discord_limits.BUTTON_LABEL
    assert rooms.CATEGORY_CHANNEL_CAP is discord_limits.CATEGORY_CHILDREN
    assert traffic.GUILD_CHANNEL_CAP is discord_limits.GUILD_CHANNELS
