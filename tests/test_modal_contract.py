"""Контракт К3: все модалки — SafeModal, defer до сервиса, ошибки в CriticalLog."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import ui

import conductor.cogs
import conductor.presentation
from conductor.bot import COGS
from conductor.domain.moderation import AppealDecision, AppealVerdict
from conductor.presentation import copy
from conductor.presentation.layout import collected_text, panel
from conductor.presentation.modal import DeferMode, SafeModal
from conductor.presentation.moderation import CaseAppealModal
from conductor.services.errors import CriticalLog

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
_ALLOWED_AWAIT = frozenset({"respond", "send_modal"})


def _import_ui_modules() -> None:
    for package in (conductor.presentation, conductor.cogs):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            importlib.import_module(module.name)
    for extension in COGS:
        importlib.import_module(extension)


def _conductor_modals() -> list[type[ui.Modal]]:
    found: list[type[ui.Modal]] = []

    def walk(cls: type[ui.Modal]) -> None:
        for sub in cls.__subclasses__():
            if sub.__module__.startswith("conductor.") and sub is not SafeModal:
                found.append(sub)
            walk(sub)

    walk(ui.Modal)
    return found


def _await_call(node: ast.expr) -> ast.Call | None:
    current = node
    if isinstance(current, ast.Await):
        current = current.value
    if isinstance(current, ast.Call):
        return current
    return None


def _is_allowed_await(node: ast.expr) -> bool:
    call = _await_call(node)
    if call is None:
        return False
    func = call.func
    if isinstance(func, ast.Name) and func.id in _ALLOWED_AWAIT:
        return True
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _ALLOWED_AWAIT:
        return True
    return isinstance(func.value, ast.Attribute) and func.value.attr == "response"


def _none_submit_violations(cls: type[SafeModal]) -> list[str]:
    path = Path(inspect.getfile(cls))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    body: ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            body = next(
                (item for item in node.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "submit"),
                None,
            )
            break
    if body is None:
        return [f"{cls.__name__}: нет submit()"]
    hits: list[str] = []
    for node in ast.walk(body):
        if isinstance(node, ast.Await) and not _is_allowed_await(node):
            hits.append(f"{cls.__name__}:{node.lineno}")
    return hits


def test_every_modal_is_safe_modal() -> None:
    _import_ui_modules()
    modals = _conductor_modals()
    assert len(modals) == 38
    bad_base = [cls.__name__ for cls in modals if not issubclass(cls, SafeModal)]
    own_hooks = [
        f"{cls.__name__}.{name}"
        for cls in modals
        for name in ("on_submit", "on_error")
        if name in cls.__dict__
    ]
    none_hits: list[str] = []
    for cls in modals:
        if cls.defer_mode is DeferMode.NONE:
            none_hits.extend(_none_submit_violations(cls))
    assert not bad_base, "не SafeModal: " + ", ".join(bad_base)
    assert not own_hooks, "on_submit/on_error в подклассе: " + ", ".join(own_hooks)
    assert not none_hits, "DeferMode.NONE с I/O в submit:\n  " + "\n  ".join(none_hits)


class _JournalResponse:
    def __init__(self, journal: list[str]) -> None:
        self._journal = journal
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._journal.append("defer")
        self._done = True

    async def send_message(self, **kwargs: object) -> None:
        self._journal.append("send_message")
        self._done = True


class _JournalFollowup:
    def __init__(self, journal: list[str], sent: list[dict[str, object]]) -> None:
        self._journal = journal
        self._sent = sent

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self._journal.append("followup.send")
        self._sent.append(kwargs)
        return SimpleNamespace(id=1)


def _interaction(
    *,
    journal: list[str],
    sent: list[dict[str, object]] | None = None,
    client: object | None = None,
    guild: object | None = None,
    user_id: int = 42,
) -> SimpleNamespace:
    payload = sent if sent is not None else []
    created = datetime.now(UTC) - timedelta(milliseconds=120)
    return SimpleNamespace(
        id=0x4173,
        response=_JournalResponse(journal),
        followup=_JournalFollowup(journal, payload),
        client=client,
        guild=guild,
        user=SimpleNamespace(id=user_id),
        command=None,
        data={"custom_id": "case-appeal"},
        created_at=created,
        type=SimpleNamespace(name="modal_submit"),
        namespace=None,
        channel=SimpleNamespace(id=2, name="mod", type=SimpleNamespace(name="text")),
    )


async def test_submit_runs_after_defer() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []

    class Probe(SafeModal, title="probe"):
        async def submit(self, interaction: discord.Interaction) -> None:
            from conductor.presentation.layout import respond

            journal.append("service")
            await respond(interaction, panel("ok", "готово"))

    interaction = _interaction(journal=journal, sent=sent, client=SimpleNamespace())
    await Probe().on_submit(interaction)  # type: ignore[arg-type]
    assert journal[0] == "defer"
    assert journal[1] == "service"
    assert journal[2] == "followup.send"


async def test_submit_error_goes_to_critical_log() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []
    posted: list[object] = []
    bot = SimpleNamespace(
        is_developer=lambda _uid: False,
        config_by_guild={9: SimpleNamespace(admin_log_channel_id=55)},
        log_pump=SimpleNamespace(submit=lambda _channel_id, view=None, **_k: posted.append(view)),
        errors=None,
    )
    bot.errors = CriticalLog(bot)

    class Boom(SafeModal, title="boom"):
        async def submit(self, interaction: discord.Interaction) -> None:
            raise RuntimeError("boom")

    guild = SimpleNamespace(id=9, name="Hall", owner_id=1, me=None)
    interaction = _interaction(journal=journal, sent=sent, client=bot, guild=guild)
    await Boom().on_submit(interaction)  # type: ignore[arg-type]
    assert journal[0] == "defer"
    assert posted
    text = collected_text(sent[0]["view"])  # type: ignore[arg-type]
    assert "E-" in text
    assert copy.ERROR_TITLE_MODAL in collected_text(posted[0])  # type: ignore[arg-type]


async def test_case_appeal_defers_before_ticket() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []
    member = SimpleNamespace(id=42)

    async def get_case(case_id: int) -> SimpleNamespace:
        journal.append("get_case")
        return SimpleNamespace(id=case_id, guild_id=1)

    async def open_case_appeal(
        guild: object,
        actor: object,
        case_id: int,
        *,
        text: str,
    ) -> AppealDecision:
        journal.append("open_case_appeal")
        assert text
        assert case_id == 7
        return AppealDecision(AppealVerdict.OK, ticket_id=9, channel_id=55)

    guild = SimpleNamespace(id=1, get_member=lambda uid: member if uid == 42 else None)
    client = SimpleNamespace(
        tickets=SimpleNamespace(open_case_appeal=open_case_appeal),
        moderation=SimpleNamespace(get_case=get_case),
        get_guild=lambda _gid: guild,
    )
    interaction = _interaction(journal=journal, sent=sent, client=client, guild=guild)
    modal = CaseAppealModal(7)
    modal.reason.component._value = "считаю наказание несправедливым полностью"
    await modal.on_submit(interaction)  # type: ignore[arg-type]
    assert journal == ["defer", "get_case", "open_case_appeal", "followup.send"]
    text = collected_text(sent[0]["view"])  # type: ignore[arg-type]
    assert copy.CASE_APPEAL_OK in text
    assert "<#55>" in text
