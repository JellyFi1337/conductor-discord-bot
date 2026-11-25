from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import discord
import pytest
from discord import ui

from conductor.cogs.diagnostics import CheckStatus, DiagnoseLine, diagnose_card, render_diagnose
from conductor.presentation.copy import (
    CONFIRM_CANCEL,
    DENIED_TITLE,
    EMPTY_TITLE,
    FAILURE_TITLE,
    WAIT_BODY,
    WAIT_TITLE,
)
from conductor.presentation.layout import (
    LayoutBudgetError,
    collected_text,
    command_failure,
    confirm,
    denied,
    disabled_copy,
    empty,
    error_code,
    event,
    failure,
    kv,
    listing,
    message_kwargs,
    paginate,
    panel,
    wait,
)
from conductor.presentation.tokens import Icon, Tone, meter

_HEX = re.compile(r"0x[0-9A-Fa-f]+")
_ENGLISH_SERVICE = re.compile(r"\b(True|False|None|NoneType|custom_id|traceback|LayoutView)\b")


def _assert_clean(text: str) -> None:
    assert "None" not in text
    assert _HEX.search(text) is None
    assert _ENGLISH_SERVICE.search(text) is None


def test_panel_signature_has_no_content_or_embed() -> None:
    names = inspect.signature(panel).parameters
    assert "content" not in names
    assert "embed" not in names
    kwargs = message_kwargs(panel("Заголовок"))
    assert "content" not in kwargs
    assert "embed" not in kwargs
    assert kwargs["allowed_mentions"].everyone is False


def test_meter_shows_bar_and_percent() -> None:
    assert meter(7, 10) == "▰▰▰▰▰▰▰▱▱▱  70%"
    assert meter(0, 10) == "▱▱▱▱▱▱▱▱▱▱  0%"
    assert meter(10, 10) == "▰▰▰▰▰▰▰▰▰▰  100%"


def test_archetype_a_command_response() -> None:
    view = panel(
        "Режим бота",
        "Бот работает на серверах из .env.",
        tone=Tone.INFO,
        fields={"Режим": "live", "Серверы": "два"},
        footnote="меняется только в .env, затем перезапуск",
    )
    text = collected_text(view)
    assert text.splitlines()[0] == f"## {Icon.PANEL} Режим бота"
    assert "Бот работает на серверах из .env." in text
    assert "**Режим:** live" in text
    assert "**Серверы:** два" in text
    assert "-# меняется только в .env, затем перезапуск" in text
    _assert_clean(text)


def test_archetype_b_panel_with_rows() -> None:
    row = ui.ActionRow(
        ui.Button(label="Переименовать", custom_id="c:vc:rename:1"),
        ui.Button(label="Удалить", style=discord.ButtonStyle.danger, custom_id="c:vc:delete:1"),
    )
    view = panel("Комната Ann", "**Владелец:** Ann", tone=Tone.INFO, rows=(row,))
    text = collected_text(view)
    assert f"## {Icon.PANEL} Комната Ann" in text
    assert "Переименовать" in text
    assert "Удалить" in text
    _assert_clean(text)


def test_archetype_c_confirm() -> None:
    view = confirm("Закрыть тикет #124?", verb="Да, закрыть", custom_id="c:tk:close:124")
    text = collected_text(view)
    assert f"## {Icon.WARN} Закрыть тикет #124?" in text
    assert "Да, закрыть" in text
    assert CONFIRM_CANCEL in text
    _assert_clean(text)


def test_archetype_d_receipt() -> None:
    view = panel(
        "Дело #128 — тайм-аут",
        tone=Tone.DANGER,
        fields={"Кому": "участник", "Срок": "2 ч", "Причина": "оскорбления в голосовом"},
        footnote="следящий Ann",
        icon=Icon.CASE,
    )
    text = collected_text(view)
    assert f"## {Icon.CASE} Дело #128 — тайм-аут" in text
    assert "**Кому:** участник" in text
    _assert_clean(text)


def test_archetype_e_listing() -> None:
    rows = [f"**@{name}** · на смене" for name in "ABCDEFGHIJKL"]
    body = listing(rows, 1, 2)
    people, _, marker = body.partition("-#")
    assert people.count("@") == 10
    assert "K" not in people and "L" not in people
    assert "страница 1/2" in marker
    view = panel("Следящие · 12", body, tone=Tone.INFO)
    _assert_clean(collected_text(view))


def test_archetype_f_event() -> None:
    view = event(
        "Сообщение удалено",
        "> текст сообщения",
        tone=Tone.DANGER,
        fields={"Автор": "участник", "Канал": "#общий"},
        source="логи участников",
        when=discord.utils.utcnow(),
    )
    text = collected_text(view)
    assert view.timeout is None
    assert not any(isinstance(item, ui.Button) for item in view.walk_children())
    assert text.splitlines()[0] == f"## {Icon.DENY} Сообщение удалено"
    assert "> текст сообщения" in text
    assert "-# логи участников ·" in text
    _assert_clean(text)


def test_event_rejects_running_tone() -> None:
    with pytest.raises(ValueError, match="статична"):
        event("Идёт", tone=Tone.RUNNING, source="логи участников")


def test_archetype_g_empty_denied_failure() -> None:
    vacant = empty("За выбранный период дел нет.", hint="попробуйте период 30 дней")
    vacant_text = collected_text(vacant)
    assert vacant_text.startswith(f"## {EMPTY_TITLE}")
    assert Icon.LOCK not in vacant_text.splitlines()[0]
    _assert_clean(vacant_text)

    shut = denied("Это делают старшие следящие и выше.", need="старший следящий")
    shut_text = collected_text(shut)
    assert f"## {Icon.LOCK} {DENIED_TITLE}" in shut_text
    assert "нужно: старший следящий" in shut_text
    _assert_clean(shut_text)

    crash = failure("E-4173")
    crash_text = collected_text(crash)
    assert f"## {Icon.DENY} {FAILURE_TITLE}" in crash_text
    assert "код E-4173" in crash_text
    _assert_clean(crash_text)

    paused = wait()
    paused_text = collected_text(paused)
    assert WAIT_TITLE in paused_text
    assert WAIT_BODY in paused_text
    _assert_clean(paused_text)


def test_paginate_keeps_pages_not_mush() -> None:
    paragraphs = [f"абзац {index} " + ("слово " * 80) for index in range(12)]
    blob = "\n".join(paragraphs)
    assert len(blob) > 4000
    pages = paginate(blob, limit=3800)
    assert len(pages) >= 2
    assert all(len(page) <= 3800 for page in pages)
    reconstructed = "\n".join(pages)
    for paragraph in paragraphs:
        assert paragraph.strip() in reconstructed
    assert "…" not in reconstructed


def test_over_forty_components_is_a_developer_error() -> None:
    rows = []
    for index in range(13):
        rows.append(
            ui.ActionRow(
                ui.Button(label="а", custom_id=f"c:x:a:{index}"),
                ui.Button(label="б", custom_id=f"c:x:b:{index}"),
                ui.Button(label="в", custom_id=f"c:x:c:{index}"),
            )
        )
    with pytest.raises(LayoutBudgetError, match="40"):
        panel("Много кнопок", rows=tuple(rows))


def test_kv_never_prints_none_or_bool_english() -> None:
    text = kv({"Флаг": True, "Пусто": None, "Число": 12})
    assert "True" not in text
    assert "None" not in text
    assert "**Флаг:** да" in text
    assert "**Пусто:** —" in text
    _assert_clean(text)


def test_error_code_is_four_hex_digits() -> None:
    code = error_code(SimpleNamespace(id=0x1A2B3C4D5E6F))  # type: ignore[arg-type]
    assert re.fullmatch(r"E-[0-9A-F]{4}", code)
    assert error_code(SimpleNamespace()) == "E-0000"  # type: ignore[arg-type]
    text = collected_text(command_failure(SimpleNamespace(id=0x4173)))  # type: ignore[arg-type]
    assert "E-4173" in text
    assert FAILURE_TITLE in text


def test_disabled_copy_greys_buttons() -> None:
    view = confirm("Удалить?", verb="Удалить", custom_id="c:x:del:1")
    disabled_copy(view)
    buttons = [item for item in view.walk_children() if isinstance(item, ui.Button)]
    assert buttons
    assert all(button.disabled for button in buttons)


def test_diagnose_card_matches_status_archetype() -> None:
    lines = [
        DiagnoseLine(CheckStatus.OK, "Интент Server Members включён"),
        DiagnoseLine(CheckStatus.FAIL, "Нет права Manage Roles", "Выдайте право в настройках сервера."),
    ]
    text = render_diagnose(lines)
    assert text.startswith(f"## {Icon.DENY} Диагностика")
    assert "✓ Интент Server Members включён" in text
    assert "✕ Нет права Manage Roles" in text
    view = diagnose_card(lines)
    assert isinstance(view, ui.LayoutView)
    kwargs = message_kwargs(view)
    assert "content" not in kwargs
