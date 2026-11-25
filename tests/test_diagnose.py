from __future__ import annotations

from conductor.domain.observe import TAB_NOW, TAB_READY, TAB_SETUP, TAB_SKELETON
from conductor.domain.seed import (
    FEATURES,
    SKELETON,
    SkeletonCheck,
    SkeletonStatus,
    feature_verdicts,
    skeleton_entries,
)
from conductor.domain.showcase import SHOWCASES, ShowcaseState
from conductor.presentation import copy
from conductor.presentation.diagnose import (
    SKELETON_PAGE_SIZE,
    CheckStatus,
    DiagnoseSnapshot,
    LogChannelView,
    build_diagnose_lines,
    build_ready_lines,
    build_skeleton_lines,
    diagnose_card,
    skeleton_page_count,
)


def _log_ok(name: str = "logs") -> LogChannelView:
    return LogChannelView(configured=True, name=name, can_send=True, can_embed=True)


def _snapshot(**overrides: object) -> DiagnoseSnapshot:
    values: dict[str, object] = {
        "members_intent": True,
        "message_content_intent": True,
        "members_loaded": 10,
        "member_count": 10,
        "permissions": {},
        "bot_top_position": 10,
        "managed_roles": (),
        "user_log": _log_ok(),
        "admin_log": _log_ok(),
        "latency_ms": 42,
        "log_backlog": 0,
        "log_sent": 0,
        "log_failed": 0,
        "log_dropped": 0,
        "log_pump_alive": True,
        "unread_count": 0,
        "unread_names": (),
    }
    values.update(overrides)
    return DiagnoseSnapshot(**values)  # type: ignore[arg-type]


def test_skeleton_lines_empty_server_paginates_and_keeps_groups() -> None:
    snap = _snapshot()
    pages = skeleton_page_count(snap)
    assert pages > 1
    titles: list[str] = []
    for page in range(1, pages + 1):
        lines = build_skeleton_lines(snap, page=page)
        assert len(lines) <= SKELETON_PAGE_SIZE
        titles.extend(line.title for line in lines)
    for _feature, copy_name, _keys in FEATURES:
        assert getattr(copy, copy_name) in titles


def test_skeleton_hint_codes_are_translated() -> None:
    checks = (SkeletonCheck("rules", "правила", SkeletonStatus.FAIL, "missing"),)
    lines = build_skeleton_lines(_snapshot(skeleton=checks))
    problem = next(line for line in lines if line.title == "правила")
    assert problem.action == copy.SKELETON_HINT_MISSING


def test_skeleton_status_marks() -> None:
    checks = tuple(
        SkeletonCheck(entry.key, entry.diagnose_label, status)
        for entry, status in zip(
            SKELETON[:3],
            (SkeletonStatus.OK, SkeletonStatus.WARN, SkeletonStatus.FAIL),
            strict=True,
        )
    )
    lines = build_skeleton_lines(_snapshot(skeleton=checks))
    by_title = {line.title: line.status for line in lines}
    warn_label = SKELETON[1].diagnose_label
    fail_label = SKELETON[2].diagnose_label
    assert by_title[warn_label] is CheckStatus.WARN
    assert by_title[fail_label] is CheckStatus.FAIL
    assert any(copy.diagnose_skeleton_rest(1) == line.title for line in lines)


def test_setup_tab_shows_log_and_evidence_lines() -> None:
    snap = _snapshot(
        staff_feed=LogChannelView(False, None, False, False),
        ticket_log=LogChannelView(False, None, False, False),
        evidence_category=False,
    )
    lines = build_diagnose_lines(snap, tab=TAB_SETUP)
    titles = "\n".join(line.title for line in lines)
    assert copy.log_ok(copy.LOG_USER, "logs") in titles
    assert copy.log_ok(copy.LOG_ADMIN, "logs") in titles
    assert copy.log_unconfigured(copy.LOG_STAFF) in titles
    assert copy.log_unconfigured(copy.LOG_TICKETS) in titles
    assert copy.DIAGNOSE_EVIDENCE_OFF in titles
    assert any(line.action == copy.DIAGNOSE_LOG_ACTION for line in lines if copy.LOG_STAFF in line.title)
    assert any(line.action == copy.DIAGNOSE_TICKET_LOG_ACTION for line in lines if copy.LOG_TICKETS in line.title)
    skeleton = "\n".join(line.title for line in build_diagnose_lines(snap, tab=TAB_SKELETON))
    assert any(entry.diagnose_label in skeleton for entry in SKELETON)


def test_setup_tab_fails_when_user_log_cannot_send() -> None:
    snap = _snapshot(user_log=LogChannelView(True, "лог-участников", False, True))
    line = next(item for item in build_diagnose_lines(snap, tab=TAB_SETUP) if copy.LOG_USER in item.title)
    assert line.status is CheckStatus.FAIL
    assert copy.log_cannot_write(copy.LOG_USER, "лог-участников") == line.title
    assert copy.LOG_PERM_SEND in line.action


def test_setup_tab_shows_schema_head() -> None:
    ok = "\n".join(
        line.title
        for line in build_diagnose_lines(_snapshot(schema_revision="0065", schema_head="0065"), tab=TAB_SETUP)
    )
    assert copy.DIAGNOSE_SCHEMA_OK in ok
    fail = build_diagnose_lines(
        _snapshot(schema_revision="0064", schema_head="0065"),
        tab=TAB_SETUP,
    )
    titles = "\n".join(line.title for line in fail)
    assert copy.diagnose_schema_fail("0064", "0065") in titles
    assert any(line.status is CheckStatus.FAIL and line.action == copy.DIAGNOSE_SCHEMA_ACTION for line in fail)


def test_now_tab_shows_cache_occupancy() -> None:
    snap = _snapshot(cache_usage=(("guild_config", 3, 1000), ("ranks", 12, 50_000)))
    titles = "\n".join(line.title for line in build_diagnose_lines(snap, tab=TAB_NOW))
    assert "Занятость кэшей" in titles
    assert "guild_config 3/1000" in titles


def test_now_tab_shows_startup_ms() -> None:
    snap = _snapshot(startup_ms=(("cogs", 12), ("panels", 4)))
    titles = "\n".join(line.title for line in build_diagnose_lines(snap, tab=TAB_NOW))
    assert copy.diagnose_startup((("cogs", 12), ("panels", 4))) in titles


def test_setup_tab_shows_voice_hub() -> None:
    ok = build_diagnose_lines(_snapshot(voice_hub="ok"), tab=TAB_SETUP)
    assert any(line.status is CheckStatus.OK and line.title == copy.DIAGNOSE_VOICE_HUB_OK for line in ok)
    fail = build_diagnose_lines(_snapshot(voice_hub="no_hub"), tab=TAB_SETUP)
    assert any(
        line.status is CheckStatus.FAIL and line.action == copy.DIAGNOSE_VOICE_HUB_ACTION for line in fail
    )
    warn = build_diagnose_lines(_snapshot(voice_hub="rate"), tab=TAB_SETUP)
    assert any(line.status is CheckStatus.WARN and line.action == copy.DIAGNOSE_VOICE_HUB_ACTION for line in warn)


def _ok_checks() -> tuple[SkeletonCheck, ...]:
    return tuple(
        SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.OK) for entry in skeleton_entries()
    )


def test_feature_verdicts_tickets_missing_does_not_break_voice() -> None:
    checks = tuple(
        SkeletonCheck(
            entry.key,
            entry.diagnose_label,
            SkeletonStatus.FAIL if entry.key == "cat_tickets" else SkeletonStatus.OK,
            "missing" if entry.key == "cat_tickets" else "",
        )
        for entry in skeleton_entries()
    )
    verdicts = {item.feature: item for item in feature_verdicts(checks)}
    assert verdicts["tickets"].status is SkeletonStatus.FAIL
    assert "cat_tickets" in verdicts["tickets"].missing
    assert verdicts["voice"].status is SkeletonStatus.OK


def test_feature_verdicts_stale_showcase_is_warn() -> None:
    verdicts = {item.feature: item for item in feature_verdicts(_ok_checks(), showcases={"tickets": "stale"})}
    assert verdicts["tickets"].status is SkeletonStatus.WARN
    assert verdicts["tickets"].note == ShowcaseState.STALE.value
    assert verdicts["voice"].status is SkeletonStatus.OK
    assert verdicts["tickets"].status is not SkeletonStatus.FAIL
    assert verdicts["tickets"].status is not SkeletonStatus.OK


def test_ready_tab_healthy_server() -> None:
    snap = _snapshot(
        skeleton=_ok_checks(),
        voice_hub="ok",
        evidence_category="ok",
        showcases={spec.id: ShowcaseState.LIVE.value for spec in SHOWCASES},
        structure_channel=True,
    )
    lines = build_ready_lines(snap)
    titles = [line.title for line in lines]
    assert copy.DIAGNOSE_READY_OK in titles
    assert copy.DIAGNOSE_VOICE_HUB_OK in titles
    assert copy.DIAGNOSE_EVIDENCE_OK in titles
    assert all(line.status is not CheckStatus.FAIL for line in lines)

    async def on_tab(_interaction: object, _tab: str) -> None:
        return None

    view = diagnose_card(lines, tab=TAB_READY, on_tab=on_tab)
    from discord import ui

    labels = [item.label for item in view.walk_children() if isinstance(item, ui.Button)]
    assert copy.DIAGNOSE_REPAIR_ALL not in labels
    assert copy.DIAGNOSE_REAPPLY not in labels
    assert copy.SETUP_MAP_OPEN not in labels
