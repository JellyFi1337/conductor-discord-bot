from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import Panel
from conductor.db.session import session_scope
from conductor.domain.access import AccessDecision, Rank
from conductor.domain.mutations import MutationScope
from conductor.presentation import persistent as persistent_mod
from conductor.presentation.copy import STALE_BODY, STALE_CARD, WAIT_BODY
from conductor.presentation.guard import GuardResource, GuardStep, guarded, run_guard
from conductor.presentation.layout import collected_text, stale
from conductor.presentation.persistent import (
    PersistentButton,
    dynamic_items,
    format_custom_id,
    load_persistent_ui,
    parse_custom_id,
)
from conductor.presentation.tokens import Icon
from conductor.services.limits import InteractionLimiter
from conductor.services.panels import record_panel, restore_panels


def test_parse_custom_id_roundtrip() -> None:
    token = format_custom_id("tk", "close", 8412)
    parsed = parse_custom_id(token)
    assert token == "c:tk:close:8412"
    assert parsed is not None
    assert parsed.kind == "tk"
    assert parsed.action == "close"
    assert parsed.ref == "8412"
    assert parsed.ref_int == 8412
    assert parsed.ref_key == "8412"
    assert str(parsed) == token


def test_parse_custom_id_accepts_slug_ref() -> None:
    token = format_custom_id("sk", "fix", "cat_evidence")
    parsed = parse_custom_id(token)
    assert token == "c:sk:fix:cat_evidence"
    assert parsed is not None
    assert parsed.kind == "sk"
    assert parsed.action == "fix"
    assert parsed.ref == "cat_evidence"
    assert parsed.ref_int is None
    assert parsed.ref_key == "cat_evidence"


def test_parse_custom_id_accepts_error_code_ref() -> None:
    token = format_custom_id("bug", "new", "E-1A2B")
    parsed = parse_custom_id(token)
    assert token == "c:bug:new:E-1A2B"
    assert parsed is not None
    assert parsed.kind == "bug"
    assert parsed.action == "new"
    assert parsed.ref == "E-1A2B"
    assert parsed.ref_int is None


def test_parse_custom_id_rejects_long_slug_ref() -> None:
    assert parse_custom_id(format_custom_id("sk", "fix", "a" * 32)) is not None
    assert parse_custom_id(format_custom_id("sk", "fix", "a" * 33)) is None


def test_panel_kinds_unique_and_cover_dynamic_items() -> None:
    from conductor.presentation.persistent import CUSTOM_ID_PATTERN, PANEL_KINDS

    kinds = set(PANEL_KINDS)
    assert len(PANEL_KINDS) == len(kinds)
    assert kinds >= {"sk", "shw", "bp"}
    for kind in PANEL_KINDS:
        assert re.fullmatch(r"[a-z]{2,4}", kind)
    assert "ref" in CUSTOM_ID_PATTERN
    items = load_persistent_ui()
    for cls in items:
        kind = _kind_of_item(cls)
        assert kind, cls
        assert kind in kinds, cls


def _kind_of_item(cls: type) -> str:
    kind = str(getattr(cls, "kind", "") or "")
    if kind:
        return kind
    compiled = getattr(cls, "__discord_ui_compiled_template__", None)
    raw = compiled.pattern if compiled is not None else ""
    match = re.search(r"c:([a-z]{2,4}):", raw)
    return "" if match is None else match.group(1)


def test_parse_custom_id_rejects_user_snowflake_scheme_and_junk() -> None:
    assert parse_custom_id("c:tk:close:not-a-id") is None
    assert parse_custom_id("tk:close:8412") is None
    assert parse_custom_id("c:ticket:close:1") is None
    assert parse_custom_id("c:tk:close:8412:extra") is None
    assert parse_custom_id("") is None


def test_base_dynamic_item_is_not_registered() -> None:
    assert PersistentButton not in dynamic_items()


def test_load_persistent_ui_registers_feature_buttons() -> None:
    from conductor.presentation.lfg import LfgNewButton
    from conductor.presentation.verification import VerifyButton
    from conductor.presentation.voice_panel import VoiceLockButton

    items = load_persistent_ui()
    assert LfgNewButton in items
    assert VerifyButton in items
    assert VoiceLockButton in items
    assert PersistentButton not in items
    assert items == load_persistent_ui()


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _Response:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, **kwargs: object) -> None:
        self.payloads.append(kwargs)
        self._done = True


def _interaction(
    *,
    guild_id: int | None = 1,
    user_id: int = 10,
    limiter: InteractionLimiter | None = None,
    decision: AccessDecision | None = None,
    custom_id: str = "c:tk:close:8412",
) -> SimpleNamespace:
    async def decide_access(**_kwargs: object) -> AccessDecision:
        return decision or AccessDecision(False, Rank.NONE, need=Rank.WATCHER)

    response = _Response()
    client = SimpleNamespace(
        interactions=limiter,
        decide_access=decide_access,
        log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
        config_by_guild=SimpleNamespace(get=lambda *_a, **_k: None),
    )
    return SimpleNamespace(
        guild=None if guild_id is None else SimpleNamespace(id=guild_id),
        user=SimpleNamespace(
            id=user_id,
            roles=(),
            guild_permissions=SimpleNamespace(administrator=False),
        ),
        client=client,
        response=response,
        data={"custom_id": custom_id},
    )


async def _load(resource: GuardResource | None):
    async def loader(*, guild_id: int, ref: str) -> GuardResource | None:
        loader.calls.append((guild_id, ref))  # type: ignore[attr-defined]
        return resource

    loader.calls = []  # type: ignore[attr-defined]
    return loader


async def test_foreign_ref_is_forged_and_does_not_reach_mutations() -> None:
    interaction = _interaction(guild_id=1, decision=AccessDecision(True, Rank.CHIEF))
    logs: list[object] = []
    interaction.client.log_pump = SimpleNamespace(submit=lambda *args, **kwargs: logs.append(kwargs))
    interaction.client.config_by_guild = SimpleNamespace(get=lambda _gid: SimpleNamespace(admin_log_channel_id=99))
    loader = await _load(GuardResource(ref="8412", guild_id=2, owner_id=10))
    verdict = await run_guard(
        interaction,  # type: ignore[arg-type]
        ref="8412",
        load=loader,
        key="tickets.close_own",
        scope=MutationScope.CHANNELS_TICKETS,
    )
    assert verdict.ok is False
    assert verdict.forged is True
    assert verdict.reason == "forged"
    assert verdict.steps == (GuardStep.GUILD, GuardStep.RESOURCE, GuardStep.ACCESS)
    assert GuardStep.HIERARCHY not in verdict.steps
    assert logs


async def test_allow_dm_checks_owner_by_ref() -> None:
    owner = _interaction(guild_id=None, user_id=10)
    loader = await _load(GuardResource(ref="4", guild_id=8, owner_id=10))
    verdict = await run_guard(
        owner,  # type: ignore[arg-type]
        ref="4",
        load=loader,
        key=None,
        allow_dm=True,
    )
    assert verdict.ok is True
    assert verdict.steps == (GuardStep.GUILD, GuardStep.RESOURCE, GuardStep.ACCESS)
    assert loader.calls == [(0, "4")]  # type: ignore[attr-defined]

    stranger = _interaction(guild_id=None, user_id=11)
    denied = await run_guard(
        stranger,  # type: ignore[arg-type]
        ref="4",
        load=loader,
        key=None,
        allow_dm=True,
    )
    assert denied.ok is False
    assert denied.reason == "denied"

    blocked = await run_guard(
        _interaction(guild_id=None, user_id=10),  # type: ignore[arg-type]
        ref="4",
        load=loader,
        key=None,
        allow_dm=False,
    )
    assert blocked.ok is False
    assert blocked.reason == "guild"


async def test_dm_click_stops_before_resource() -> None:
    interaction = _interaction(guild_id=None)
    loader = await _load(GuardResource(ref="1", guild_id=1, owner_id=10))
    verdict = await run_guard(
        interaction,  # type: ignore[arg-type]
        ref="1",
        load=loader,
        key="tickets.close_own",
    )
    assert verdict.ok is False
    assert verdict.reason == "guild"
    assert verdict.steps == (GuardStep.GUILD,)
    assert loader.calls == []  # type: ignore[attr-defined]


async def test_missing_resource_is_stale_card() -> None:
    interaction = _interaction(decision=AccessDecision(True, Rank.CHIEF))
    loader = await _load(None)
    verdict = await run_guard(
        interaction,  # type: ignore[arg-type]
        ref="1",
        load=loader,
        key="tickets.close_own",
    )
    assert verdict.ok is False
    assert verdict.reason == "stale"
    assert verdict.steps == (GuardStep.GUILD, GuardStep.RESOURCE)
    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert STALE_CARD in text


async def test_closed_resource_is_stale_card() -> None:
    interaction = _interaction(decision=AccessDecision(True, Rank.CHIEF))
    loader = await _load(GuardResource(ref="1", guild_id=1, closed=True, owner_id=10))
    verdict = await run_guard(
        interaction,  # type: ignore[arg-type]
        ref="1",
        load=loader,
        key="tickets.close_own",
        scope=MutationScope.CHANNELS_TICKETS,
    )
    assert verdict.ok is False
    assert verdict.reason == "stale"
    assert verdict.steps == (GuardStep.GUILD, GuardStep.RESOURCE)
    view = interaction.response.payloads[0]["view"]
    text = collected_text(view)  # type: ignore[arg-type]
    assert STALE_CARD in text
    assert STALE_BODY in text


async def test_limiter_fires_before_resource_load() -> None:
    clock = _Clock()
    limiter = InteractionLimiter(limit=1, window=60, clock=clock)
    interaction = _interaction(limiter=limiter, decision=AccessDecision(True, Rank.CHIEF))
    loader = await _load(GuardResource(ref="1", guild_id=1, owner_id=10))
    first = await run_guard(interaction, ref="1", load=loader, key="tickets.close_own")  # type: ignore[arg-type]
    assert first.ok is True
    assert loader.calls  # type: ignore[attr-defined]
    loader.calls.clear()  # type: ignore[attr-defined]
    interaction.response = _Response()
    second = await run_guard(interaction, ref="1", load=loader, key="tickets.close_own")  # type: ignore[arg-type]
    assert second.ok is False
    assert second.reason == "wait"
    assert second.steps == (GuardStep.GUILD,)
    assert loader.calls == []  # type: ignore[attr-defined]
    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert WAIT_BODY in text


async def test_guard_steps_run_in_declared_order() -> None:
    interaction = _interaction(
        user_id=10,
        decision=AccessDecision(False, Rank.NONE, need=Rank.WATCHER),
    )
    loader = await _load(GuardResource(ref="5", guild_id=1, owner_id=99))
    denied = await run_guard(
        interaction,  # type: ignore[arg-type]
        ref="5",
        load=loader,
        key="tickets.close_any",
        scope=MutationScope.CHANNELS_TICKETS,
        hierarchy=True,
    )
    assert denied.reason == "denied"
    assert denied.steps == (GuardStep.GUILD, GuardStep.RESOURCE, GuardStep.ACCESS)

    owner_bot = _interaction(
        user_id=10,
        decision=AccessDecision(False, Rank.WATCHER),
    )
    owner_loader = await _load(GuardResource(ref="5", guild_id=1, owner_id=10, target_rank=Rank.NONE))
    owned = await run_guard(
        owner_bot,  # type: ignore[arg-type]
        ref="5",
        load=owner_loader,
        key="tickets.close_own",
        scope=MutationScope.CHANNELS_TICKETS,
        hierarchy=True,
    )
    assert owned.ok is True

    open_bot = _interaction(
        user_id=10,
        decision=AccessDecision(True, Rank.WATCHER),
    )
    high = await _load(GuardResource(ref="5", guild_id=1, owner_id=10, target_rank=Rank.CHIEF))
    hierarchy = await run_guard(
        open_bot,  # type: ignore[arg-type]
        ref="5",
        load=high,
        key="tickets.close_own",
        scope=MutationScope.CHANNELS_TICKETS,
        hierarchy=True,
    )
    assert hierarchy.reason == "hierarchy"
    assert hierarchy.steps == (
        GuardStep.GUILD,
        GuardStep.RESOURCE,
        GuardStep.ACCESS,
        GuardStep.HIERARCHY,
    )


async def test_public_guard_allows_stranger_without_key() -> None:
    interaction = _interaction(user_id=99, decision=AccessDecision(False, Rank.NONE, need=Rank.SENIOR))
    loader = await _load(GuardResource(ref="1", guild_id=1, owner_id=10))
    verdict = await run_guard(
        interaction,  # type: ignore[arg-type]
        ref="1",
        load=loader,
        key=None,
        public=True,
    )
    assert verdict.ok is True
    assert verdict.reason is None
    assert verdict.steps == (GuardStep.GUILD, GuardStep.RESOURCE, GuardStep.ACCESS)


async def test_public_guard_still_rejects_forged_ref() -> None:
    interaction = _interaction(user_id=99, decision=AccessDecision(True, Rank.CHIEF))
    loader = await _load(GuardResource(ref="1", guild_id=2, owner_id=10))
    verdict = await run_guard(
        interaction,  # type: ignore[arg-type]
        ref="1",
        load=loader,
        key=None,
        public=True,
    )
    assert verdict.ok is False
    assert verdict.forged is True
    assert verdict.reason == "forged"


async def test_guarded_decorator_skips_action_on_stale() -> None:
    calls: list[object] = []

    class Item:
        ref = "1"
        access_key = "tickets.close_own"
        mutation_scope = None
        checks_hierarchy = False

        async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
            return None

        @guarded()
        async def on_click(self, _interaction: object, verdict: object) -> None:
            calls.append(verdict)

    interaction = _interaction()
    await Item().on_click(interaction)
    assert calls == []
    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert STALE_CARD in text


def test_stale_card_is_muted() -> None:
    text = collected_text(stale())
    assert text.splitlines()[0] == f"## {Icon.LOCK} {STALE_CARD}"
    assert STALE_BODY in text


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


BOT_PY = Path(__file__).resolve().parents[1] / "src" / "conductor" / "bot.py"

_TIMER_RESTORES = (
    "giveaways",
    "polls",
    "gatherings",
    "groups",
    "reminders",
    "ladder",
)


def _restore_targets(tree: ast.AST, method: str) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != method:
            continue
        names: set[str] = set()
        for child in ast.walk(node):
            func = child.func if isinstance(child, ast.Call) else None
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "restore"
                and isinstance(func.value, ast.Attribute)
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "self"
            ):
                names.add(func.value.attr)
        return names
    raise AssertionError(f"метод {method} не найден")


def test_static_view_registry_is_gone() -> None:
    assert not hasattr(persistent_mod, "register_static_view")
    assert not hasattr(persistent_mod, "static_view_factory")
    assert not hasattr(persistent_mod, "attach_static_view")


def test_timer_restore_only_in_setup_hook() -> None:
    tree = ast.parse(BOT_PY.read_text(encoding="utf-8"), filename=str(BOT_PY))
    setup = _restore_targets(tree, "setup_hook")
    ready = _restore_targets(tree, "on_ready")
    missing = [name for name in _TIMER_RESTORES if name not in setup]
    assert not missing, missing
    assert ready == set()


async def test_missing_message_marks_panel_stale(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(sessions=sessions, get_channel=lambda *_: None, add_view=lambda *_a, **_k: None)
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="tk",
                guild_id=1,
                channel_id=20,
                message_id=30,
                ref=8412,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async def probe(_channel_id: int, _message_id: int) -> bool:
            return False

        report = await restore_panels(bot, probe=probe)  # type: ignore[arg-type]
        assert report.live == 0
        assert report.stale == 1
        async with session_scope(sessions) as session:
            row = await session.scalar(select(Panel))
            assert row is not None
            assert row.state == "stale"
    finally:
        await engine.dispose()


async def test_live_message_keeps_panel(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    views: list[object] = []
    bot = SimpleNamespace(
        sessions=sessions,
        get_channel=lambda *_: None,
        add_view=lambda *_a, **_k: views.append((_a, _k)),
    )
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="vc",
                guild_id=1,
                channel_id=20,
                message_id=31,
                ref=4,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async def probe(_channel_id: int, _message_id: int) -> bool:
            return True

        report = await restore_panels(bot, probe=probe)  # type: ignore[arg-type]
        assert report.live == 1
        assert report.stale == 0
        assert views == []
        async with session_scope(sessions) as session:
            row = await session.scalar(select(Panel))
            assert row is not None
            assert row.state == "live"
    finally:
        await engine.dispose()


def _persistent_subclasses() -> list[type]:
    import importlib
    import pkgutil

    import conductor.presentation as presentation
    from conductor.presentation.persistent import PersistentButton

    for module in pkgutil.iter_modules(presentation.__path__):
        if module.name != "copy":
            importlib.import_module(f"conductor.presentation.{module.name}")
    found: list[type] = []
    stack = list(PersistentButton.__subclasses__())
    while stack:
        cls = stack.pop()
        found.append(cls)
        stack.extend(cls.__subclasses__())
    return found


def test_no_persistent_button_is_always_stale() -> None:
    """Кнопка без своего load_resource и без своего callback на любое нажатие отвечает «устарело»."""
    from conductor.presentation.persistent import PersistentButton

    dead = sorted(
        f"{cls.__module__}.{cls.__name__}"
        for cls in _persistent_subclasses()
        if cls.load_resource is PersistentButton.load_resource and cls.callback is PersistentButton.callback
    )
    assert dead == []


async def test_repair_buttons_load_guild_resource() -> None:
    from conductor.presentation.moderation import EvidenceFixButton
    from conductor.presentation.structure import StructureFixButton
    from conductor.presentation.voice_panel import VoiceHubFixButton

    cases = ((StructureFixButton, "cat_evidence"), (VoiceHubFixButton, "hub"), (EvidenceFixButton, "cat_evidence"))
    for cls, ref in cases:
        resource = await cls(ref).load_resource(guild_id=42, ref=ref)
        assert resource is not None and resource.guild_id == 42 and not resource.closed
        assert await cls(ref).load_resource(guild_id=42, ref="nope_not_a_key") is None
        assert await cls(ref).load_resource(guild_id=0, ref=ref) is None
