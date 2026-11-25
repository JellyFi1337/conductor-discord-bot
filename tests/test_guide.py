from __future__ import annotations

from conductor.domain.access import (
    ACTION_DEFAULTS,
    ACTION_KEYS,
    MOD_WARN,
    TICKETS_CLAIM,
    AccessSnapshot,
    PermissionRule,
    Rank,
    can,
    empty_rank_roles,
    parse_rank_list,
)
from conductor.domain.guide import (
    KIND_BUTTON,
    KIND_COMMAND,
    PARTICIPANT_WHO,
    SECTION_IDS,
    START,
    TAB_MEMO,
    TAB_MINE,
    TAB_START,
    entry_visible,
    memo_entries,
    page_size_for,
    paged_entries,
    tab_entries,
    visible_entries,
)
from conductor.presentation import copy
from conductor.presentation.copy.guide_catalog import (
    catalog_entries,
    catalog_paths,
    section_commands,
)
from conductor.presentation.guide import help_card, help_entry_line, help_who, memo_body, mine_body
from conductor.presentation.layout import TEXT_LIMIT, collected_text
from conductor.services.guide import for_tab


def _snapshot(**rules: PermissionRule) -> AccessSnapshot:
    return AccessSnapshot(rank_roles=empty_rank_roles(), rules=rules, special_locked=True)


def test_help_is_open_to_everyone() -> None:
    assert "help.read" in ACTION_KEYS
    assert can(Rank.NONE, key="help.read") is True
    assert can(Rank.WATCHER, key="help.read") is True


def test_catalog_covers_every_leaf_and_has_copy() -> None:
    paths = catalog_paths()
    assert paths
    assert all(path.startswith("/") for path in paths)
    for item in catalog_entries():
        assert item.summary.strip(), item.path
        assert item.details, item.path
        assert item.kind in {KIND_COMMAND, KIND_BUTTON}


def test_catalog_keys_are_in_the_registry() -> None:
    keys = {item.key for item in catalog_entries() if item.key is not None}
    assert keys <= set(ACTION_KEYS)
    for key in keys:
        assert key in ACTION_DEFAULTS


def test_live_rights_hide_command_when_key_moves() -> None:
    snapshot = _snapshot(**{MOD_WARN: PermissionRule(MOD_WARN, frozenset({Rank.CHIEF, Rank.SPECIAL}))})
    entries = catalog_entries()
    watcher = {item.path for item in visible_entries(snapshot, Rank.WATCHER, entries=entries)}
    chief = {item.path for item in visible_entries(snapshot, Rank.CHIEF, entries=entries)}
    assert "/mod warn" not in watcher
    assert "/mod warn" in chief
    assert "/mod timeout" in watcher
    assert for_tab(snapshot, Rank.WATCHER, TAB_MINE)
    assert "/mod warn" not in {item.path for item in for_tab(snapshot, Rank.WATCHER, TAB_MINE)}


def test_memo_has_no_staff_only_commands() -> None:
    snapshot = _snapshot()
    memo = memo_entries(snapshot, catalog_entries())
    paths = [item.path for item in memo]
    assert "/remind" in paths
    assert "/help" in paths
    assert "/profile" in paths
    assert "/voice" in paths
    assert "Хочу войти" in paths
    assert "Выбрать роли" in paths
    assert "/mod warn" not in paths
    assert "/admin panel" not in paths
    assert "/watchers duty" not in paths
    assert "/owner wipe" not in paths
    for item in memo:
        if item.who in PARTICIPANT_WHO:
            continue
        if item.key is None:
            continue
        assert can(Rank.NONE, allowed=snapshot.rule(item.key).ranks), item.path
        blob = " ".join((item.summary, *item.details, *(f"{n} {d}" for n, d in item.params)))
        assert "ключ" not in blob.lower(), item.path


def test_owner_commands_only_for_developer() -> None:
    snapshot = _snapshot()
    entries = catalog_entries()
    regular = {item.path for item in visible_entries(snapshot, Rank.SPECIAL, entries=entries)}
    owner = {item.path for item in visible_entries(snapshot, Rank.NONE, entries=entries, developer=True)}
    assert "/owner wipe" not in regular
    assert "/owner wipe" in owner
    assert "/owner lift" in owner
    assert "/owner rep" in owner
    assert "/owner emojis" in owner
    assert "/owner limits" in owner


def test_help_pages_fit_layout() -> None:
    snapshot = _snapshot()
    assert tuple(copy.HELP_SECTION_LABELS) == SECTION_IDS

    async def on_tab(_interaction: object, _tab: str) -> None:
        return None

    async def on_page(_interaction: object, _page: int) -> None:
        return None

    for rank in Rank:
        show_start = rank is Rank.SPECIAL
        for tab in (TAB_MINE, TAB_MEMO, TAB_START):
            if tab == TAB_START and not show_start:
                continue
            entries = tab_entries(snapshot, rank, tab, entries=catalog_entries())
            size = page_size_for(tab)
            _, _, pages = paged_entries(entries, 1, size=size)
            if tab == TAB_START:
                pages = 1
            for page in range(1, pages + 1):
                piece, current, total = paged_entries(entries, page, size=size)
                if tab == TAB_START:
                    text_body = copy.HELP_START
                elif tab == TAB_MEMO:
                    text_body = memo_body(piece)
                else:
                    text_body = mine_body(piece)
                view = help_card(
                    tab=tab,
                    rank=rank,
                    body=text_body,
                    page=current,
                    pages=total,
                    named_rank=rank is not Rank.NONE,
                    show_start=show_start,
                    on_tab=on_tab,
                    on_page=on_page,
                )
                text = collected_text(view)
                assert len(text) < TEXT_LIMIT, f"{rank.key}:{tab}:{page}"
                assert "0x" not in text
                assert "traceback" not in text
                assert copy.HELP_TAB_MINE in text or copy.HELP_TAB_MEMO in text


def test_start_tab_lists_setup_steps() -> None:
    view = help_card(tab=TAB_START, rank=Rank.SPECIAL, body=copy.HELP_START, show_start=True)
    text = collected_text(view)
    assert "/admin" in text
    assert "Каркас" in text
    assert "Витрины" in text
    assert "/diagnose" in text
    assert "/pulse retire" not in text
    assert "/support" not in text
    assert "приветствия" not in text
    assert "automod.yml" in text
    assert "Выгнать обоих ботов" in text
    assert "/stats" in text
    assert "/owner seed" in text
    assert text.index("/diagnose") < text.index("Собрать сервер")
    assert text.index("Собрать сервер") < text.index("Витрины")
    assert copy.RANK_SPECIAL in text
    assert copy.RANK_CHIEF in text
    assert copy.RANK_SENIOR in text
    assert copy.RANK_WATCHER in text
    assert section_commands(START) == ()


def test_features_catalog_matches_command_tree() -> None:
    from pathlib import Path

    from tests.test_commands import EXPECTED_LEAVES

    text = Path("docs/FEATURES.md").read_text(encoding="utf-8")
    start = text.index("## 7. Каталог команд")
    end = text.index("## 8. Каркас сервера")
    catalog = text[start:end]
    found = set(_backticked_commands(catalog))
    assert found == set(EXPECTED_LEAVES)
    assert "контекстное меню" in catalog.lower()
    assert "Досье" in catalog
    live = text[: text.index("## 10. Границы")]
    leaves = set(EXPECTED_LEAVES)
    extras = {
        path
        for path in _backticked_commands(live)
        if path not in leaves and not any(leaf.startswith(f"{path} ") for leaf in leaves)
    }
    assert extras == set()


def _backticked_commands(blob: str) -> tuple[str, ...]:
    import re

    names: list[str] = []
    for match in re.finditer(r"`(/[a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)?)`", blob):
        path = match.group(1)
        if path not in names:
            names.append(path)
    return tuple(names)


def test_help_tabs_use_rank_custom_ids() -> None:
    async def on_tab(_interaction: object, _tab: str) -> None:
        return None

    view = help_card(
        tab=TAB_MINE,
        rank=Rank.CHIEF,
        body="/mod warn",
        named_rank=True,
        show_start=False,
        on_tab=on_tab,
    )
    ids = [getattr(item, "custom_id", None) for item in view.walk_children()]
    assert f"c:hp:{TAB_MINE}:{int(Rank.CHIEF)}" in ids
    assert f"c:hp:{TAB_MEMO}:{int(Rank.CHIEF)}" in ids
    assert f"c:hp:{TAB_START}:{int(Rank.CHIEF)}" not in ids
    text = collected_text(view)
    assert copy.HELP_TAB_MINE in text
    assert copy.help_title(copy.rank_label(Rank.CHIEF.key)) in text


def test_who_uses_registry_and_overrides() -> None:
    appoint = next(item for item in section_commands("staff") if item.path == "/watchers appoint")
    remind = next(item for item in section_commands("life") if item.path == "/remind")
    deploy = next(item for item in section_commands("data") if item.path == "/owner emojis")
    wipe = next(item for item in section_commands("data") if item.path == "/owner wipe")
    assert copy.RANK_CHIEF in help_who(appoint)
    assert copy.RANK_SENIOR in help_who(appoint)
    assert help_who(remind) == copy.HELP_WHO_ALL
    assert help_who(deploy) == copy.HELP_WHO["owner"]
    assert help_who(wipe) == copy.HELP_WHO["bot_owner"]
    claim = next(item for item in catalog_entries() if item.key == TICKETS_CLAIM)
    assert copy.RANK_SENIOR in help_who(claim)
    old = _snapshot(
        **{TICKETS_CLAIM: PermissionRule(TICKETS_CLAIM, parse_rank_list("watcher, senior, chief, special"))}
    )
    assert copy.RANK_WATCHER in help_who(claim, old)


def test_lines_mark_commands_and_buttons() -> None:
    remind = next(item for item in catalog_entries() if item.path == "/remind")
    ask = next(item for item in catalog_entries() if item.path == "Хочу войти")
    assert help_entry_line(remind).startswith("**/remind**")
    assert copy.HELP_MARK_BUTTON in help_entry_line(ask)
    assert entry_visible(ask, Rank.NONE, _snapshot()) is True


def test_special_card_has_start_tab() -> None:
    async def on_tab(_interaction: object, _tab: str) -> None:
        return None

    view = help_card(
        tab=TAB_MINE,
        rank=Rank.SPECIAL,
        body="ok",
        show_start=True,
        on_tab=on_tab,
    )
    ids = [getattr(item, "custom_id", None) for item in view.walk_children()]
    assert f"c:hp:{TAB_START}:{int(Rank.SPECIAL)}" in ids
