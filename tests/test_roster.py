from __future__ import annotations

from types import SimpleNamespace

from tests.support.attachments import assert_attachments_resolved

from conductor.domain.discord_limits import COMPONENTS_PER_MESSAGE, TEXT_DISPLAY_TOTAL
from conductor.presentation.layout import collected_text, message_kwargs
from conductor.presentation.roster import ROSTER_PAGE, RosterView, roster_preview


def test_roster_preview_shows_first_rows_and_rest() -> None:
    ids = list(range(1, 16))
    text = roster_preview(ids, 15, limit=10)
    assert "<@1>" in text
    assert "<@10>" in text
    assert "<@11>" not in text
    assert "и ещё 5" in text


def test_roster_paginates_thousand_without_pinging() -> None:
    ids = list(range(1, 1001))
    view = RosterView("Иду", ids, guild=SimpleNamespace(get_member=lambda uid: SimpleNamespace(display_name=f"u{uid}")))
    assert view.pages == 25
    assert view.allowed_mentions.everyone is False
    users = view.allowed_mentions.users
    assert not users or users is False
    for page in range(view.pages):
        view.page = page
        view._mount()
        text = collected_text(view)
        assert len(text) <= TEXT_DISPLAY_TOTAL
        assert view.total_children_count <= COMPONENTS_PER_MESSAGE
        start = page * ROSTER_PAGE
        chunk = ids[start : start + ROSTER_PAGE]
        assert f"<@{chunk[0]}>" in text
        assert f"<@{chunk[-1]}>" in text
    kwargs = message_kwargs(view.file_card())
    assert_attachments_resolved(kwargs)
    files = list(kwargs.get("files") or ())
    assert files
    payload = files[0].fp.read().decode("utf-8")
    assert "u1 (1)" in payload
    assert "u1000 (1000)" in payload
    assert payload.count("\n") == 999
