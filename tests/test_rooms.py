from __future__ import annotations

from conductor.domain.rooms import (
    HubPreset,
    HubSettings,
    dump_hub_settings,
    parse_hub_settings,
    prepare_room_name,
    render_room_name,
    strip_seq_placeholder,
    template_has_numbering,
)


def test_legacy_sequential_json_becomes_name_template() -> None:
    parsed = parse_hub_settings('{"preset": "sequential", "template": "{n} · {username}", "seq": 12}')
    assert parsed.preset == HubPreset.NAME
    assert parsed.template == "{username}"
    dumped = dump_hub_settings(parsed)
    assert "seq" not in dumped
    assert "sequential" not in dumped
    assert "{n}" not in dumped


def test_strip_seq_placeholder_drops_number_and_separator() -> None:
    assert strip_seq_placeholder("{n} · {username}") == "{username}"
    assert strip_seq_placeholder("{username} · {n}") == "{username}"
    assert strip_seq_placeholder("{n}") == "{username}"


def test_template_with_n_is_rejected() -> None:
    assert template_has_numbering("{n} · {username}") is True
    assert template_has_numbering("{username}") is False


def test_prepared_names_have_no_ordinals() -> None:
    name, _updated = prepare_room_name(HubSettings(preset="sequential", template="{n} · Ann"), username="Ann", game="")
    assert "{n}" not in name
    assert not any(ch.isdigit() for ch in name)
    assert render_room_name("{username}", username="Ann", game="") == "Ann"
