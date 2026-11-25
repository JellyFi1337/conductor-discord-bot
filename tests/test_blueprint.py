"""Файл настройки: реестр полей, разбор, план, применение."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.test_admin import _bot, _ctx

from conductor.db.session import session_scope
from conductor.domain.access import ACTION_KEYS, CONFIG_MANAGE, Rank, empty_rank_roles
from conductor.domain.blueprint import (
    BLUEPRINT,
    BLUEPRINT_MAX_BYTES,
    BLUEPRINT_VERSION,
    IssueLevel,
    attachment_error,
    parse_blueprint,
    plan_blueprint,
    put_path,
    strip_docs,
)
from conductor.domain.seed import SKELETON
from conductor.domain.showcase import SHOWCASES
from conductor.presentation import copy
from conductor.services.access import ensure_defaults
from conductor.services.admin_pages import all_pages
from conductor.services.blueprint import BlueprintService
from conductor.services.guild_config import get_or_create_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "data" / "blueprint.example.json"


def test_strip_docs_drops_underscore_keys_recursively() -> None:
    raw = {
        "_справка": {"версия_формата": 9},
        "общее": {
            "_описание": "x",
            "часовой_пояс": "UTC",
            "_значения": ["UTC"],
            "вложенное": {"_секрет": 1, "оставить": 2},
        },
    }
    cleaned = strip_docs(raw)
    assert "_справка" not in cleaned
    assert cleaned["общее"]["часовой_пояс"] == "UTC"
    assert "_описание" not in cleaned["общее"]
    assert "_значения" not in cleaned["общее"]
    assert cleaned["общее"]["вложенное"] == {"оставить": 2}


def test_attachment_rejected_before_body() -> None:
    assert attachment_error(size=BLUEPRINT_MAX_BYTES + 1, content_type="application/json") == "too_large"
    assert attachment_error(size=12, content_type="image/png") == "bad_content_type"
    assert attachment_error(size=12, content_type="application/json") is None


def test_each_error_kind_is_reachable() -> None:
    codes: set[str] = set()
    _, bad_json = parse_blueprint({"_справка": {"версия_формата": BLUEPRINT_VERSION + 5}})
    codes.update(item.message for item in bad_json)
    _, typed = parse_blueprint({"функции": {"сигналы": {"состояние": "yes"}}})
    codes.update(item.message for item in typed)
    _, snow = parse_blueprint({"функции": {"логи": {"участники": "abc"}}})
    codes.update(item.message for item in snow)
    _, rng = parse_blueprint({"функции": {"логи": {"хранить": 99999}}})
    codes.update(item.message for item in rng)
    _, choice = parse_blueprint({"общее": {"часовой_пояс": "Not/A_Zone"}})
    codes.update(item.message for item in choice)
    ranks = empty_rank_roles()
    ranks[Rank.WATCHER] = (11,)
    plan = plan_blueprint(
        {},
        {"роли": {"role_chief": {"id": 11, "имя": "x"}, "role_watcher": {"id": 11, "имя": "y"}}},
        resolved={"роли.role_chief": 11, "роли.role_watcher": 11},
        rank_roles=ranks,
    )
    codes.update(item.message for item in plan.issues)
    lock = plan_blueprint(
        {},
        {"права": {"ключи": {CONFIG_MANAGE: ["watcher"]}}},
        resolved={},
        rank_roles=empty_rank_roles(),
        actor=Rank.SPECIAL,
    )
    codes.update(item.message for item in lock.issues)
    spec = next(item for item in BLUEPRINT if item.required)
    _, required = parse_blueprint({})
    req_plan = plan_blueprint({}, {spec.path.split(".")[0]: None if "." not in spec.path else {}}, resolved={})
    nested: dict = {}
    put_path(nested, spec.path, None)
    req_plan = plan_blueprint({}, nested, resolved={})
    codes.update(item.message for item in req_plan.issues)
    assert "bad_version" in codes
    assert "bad_type" in codes
    assert "not_snowflake" in codes
    assert "out_of_range" in codes
    assert "bad_choice" in codes
    assert "rank_taken" in codes
    assert "lockout" in codes
    assert "required_null" in codes


def test_unknown_paths_do_not_block() -> None:
    wanted = {"общее": {"часовой_пояс": "UTC"}, "лишнее": {"поле": 1}}
    plan = plan_blueprint({"общее": {"часовой_пояс": "UTC"}}, wanted, resolved={})
    assert plan.blocked is False
    assert "лишнее.поле" in plan.unknown


def test_example_parses_without_error_and_covers_required() -> None:
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    body, issues = parse_blueprint(raw)
    assert not any(item.level is IssueLevel.ERROR for item in issues)
    for spec in BLUEPRINT:
        if not spec.required:
            continue
        node: object = body
        for part in spec.path.split("."):
            assert isinstance(node, dict) and part in node, spec.path
            node = node[part]
        assert node is not None, spec.path


async def test_blueprint_covers_admin_fields(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        ctx = _ctx(bot)
        bot.seed = SimpleNamespace(diagnose_checks=AsyncMock(return_value=()), snapshot=AsyncMock(return_value={}))
        bot.showcase = SimpleNamespace(statuses=AsyncMock(return_value={}), bound_ids=AsyncMock(return_value={}))
        bot.lfg = SimpleNamespace(settings=AsyncMock(return_value=None), set_channel=AsyncMock())
        labels = {spec.label for spec in BLUEPRINT}
        for page in all_pages():
            snap = await page.snapshot(ctx, 1)
            for name in snap.fields:
                assert _covered(page.id, name, labels), (page.id, name)
    finally:
        await engine.dispose()


def _covered(page_id: str, name: str, labels: set[str]) -> bool:
    if name in labels:
        return True
    if any(label and label in name for label in labels):
        return True
    if page_id == "perms" and name in ACTION_KEYS:
        return True
    if page_id in {"skeleton", "showcase"} and name in {item.label for item in SHOWCASES}:
        return True
    if page_id == "skeleton":
        if name in {copy.DIAGNOSE_READY_OK, copy.DIAGNOSE_READY_FAIL}:
            return True
        if "готов" in name:
            return True
        if any(entry.diagnose_label in name for entry in SKELETON):
            return True
    return False


async def test_export_roundtrip_is_noop(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        guild = _guild()
        bot.seed = SimpleNamespace(snapshot=AsyncMock(return_value={}))
        service = BlueprintService(bot)
        raw = await service.export(guild, with_docs=False)
        body, issues = parse_blueprint(json.loads(raw.decode("utf-8")))
        assert not any(item.level is IssueLevel.ERROR for item in issues)
        current = await service.snapshot_payload(guild)
        plan = plan_blueprint(current, body, resolved={})
        assert plan.changes == ()
    finally:
        await engine.dispose()


async def test_apply_twice_is_noop(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        async with session_scope(bot.sessions) as session:
            await ensure_defaults(session, 1)
            row = await get_or_create_config(session, 1)
            row.timezone = "UTC"
        guild = _guild()
        bot.seed = SimpleNamespace(snapshot=AsyncMock(return_value={}), bind_existing=AsyncMock(), run=AsyncMock())
        service = BlueprintService(bot)
        wanted = {"общее": {"часовой_пояс": "Europe/Moscow"}}
        first = await service.preview(guild, json.dumps(wanted).encode("utf-8"))
        report = await service.apply(guild, first, actor_id=2, wanted=wanted)
        assert "общее.часовой_пояс" in report.applied
        again = await service.preview(guild, json.dumps(wanted).encode("utf-8"))
        second = await service.apply(guild, again, actor_id=2, wanted=wanted)
        assert second.applied == ()
    finally:
        await engine.dispose()


async def test_foreign_guild_id_is_error(tmp_path: Path) -> None:
    bot, engine = await _bot(tmp_path)
    try:
        foreign = SimpleNamespace(id=99, type="text", guild=SimpleNamespace(id=99))
        guild = _guild()
        guild.get_channel = lambda channel_id: foreign if int(channel_id) == 42 else None
        bot.seed = SimpleNamespace(snapshot=AsyncMock(return_value={}))
        service = BlueprintService(bot)
        raw = json.dumps({"функции": {"логи": {"участники": 42}}}).encode("utf-8")
        plan = await service.preview(guild, raw)
        assert any(item.message == "foreign_guild" and item.level is IssueLevel.ERROR for item in plan.issues)
        report = await service.apply(guild, plan, actor_id=2, wanted={"функции": {"логи": {"участники": 42}}})
        assert "функции.логи.участники" in report.failed or plan.blocked
        async with session_scope(bot.sessions) as session:
            row = await get_or_create_config(session, 1)
            assert row.user_log_channel_id != 42
    finally:
        await engine.dispose()


def _guild() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="test",
        channels=(),
        roles=(),
        get_channel=lambda _channel_id: None,
        get_role=lambda _role_id: None,
    )
