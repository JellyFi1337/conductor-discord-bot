from types import SimpleNamespace

from conductor.presentation.text import field_value, quote_block
from conductor.services.history import skip_ids_from_config


def test_quote_block_is_readable_and_escapes_fences() -> None:
    assert quote_block("") == "*пусто*"
    assert quote_block("   ") == "*пусто*"
    assert quote_block("hello") == "> hello"
    assert "'''" in quote_block("```exploit")
    assert "```" not in quote_block("```exploit")
    assert quote_block("a\nb") == "> a\n> b"


def test_field_value_never_empty() -> None:
    assert field_value("") == "—"
    assert field_value(None) == "—"
    assert field_value(0) == "0"
    assert field_value(" текст ") == "текст"


def test_ingest_skips_configured_log_channels() -> None:
    assert skip_ids_from_config(None) == set()
    config = SimpleNamespace(
        user_log_channel_id=11,
        admin_log_channel_id=22,
        staff_feed_channel_id=33,
        automod_channel_id=44,
    )
    assert skip_ids_from_config(config) == {11, 22, 33, 44}
    assert skip_ids_from_config(SimpleNamespace(user_log_channel_id=None, admin_log_channel_id=5)) == {5}
