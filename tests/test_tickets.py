from __future__ import annotations

import asyncio
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import func, select, text

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import (
    ModCase,
    Reputation,
    ReputationEvent,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketPanel,
    TicketTranscript,
)
from conductor.db.session import session_scope
from conductor.domain.access import AccessSnapshot, Rank, empty_rank_roles
from conductor.domain.activity import EventKind
from conductor.domain.moderation import AppealVerdict, ExplainVerdict, ModVerdict
from conductor.domain.reputation import KIND_REVOKE, START, WARN_HIT
from conductor.domain.tickets import (
    ANON_CHANNEL_AUTHOR,
    DEFAULT_CATEGORIES,
    DEFAULT_CATEGORY_SPECS,
    AutocloseAction,
    CategoryKind,
    CategorySpec,
    ClaimVerdict,
    CloseVerdict,
    EscalateVerdict,
    FlowAction,
    FlowRole,
    OpenStep,
    OpenVerdict,
    Outcome,
    RateVerdict,
    TicketClosed,
    TicketEventKind,
    TicketSettings,
    TicketState,
    allowed_outcomes,
    autoclose_action,
    can_appeal,
    can_open_ticket,
    collect_form_fields,
    default_visible_ranks,
    dm_fail_due,
    dump_form_answers,
    dump_ticket_form,
    dump_ticket_settings,
    escalate_allowed,
    format_form_transcript,
    has_explain,
    is_staff_relay_skip,
    may_close,
    may_edit_reason,
    may_rate,
    may_see_notes,
    next_ticket_number,
    open_step,
    open_verdict,
    parse_form_answers,
    parse_ticket_form,
    parse_ticket_settings,
    rank_may_see,
    ticket_channel_author,
    ticket_channel_name,
    transition,
    visible_rank_set,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import dynamic_items, parse_custom_id
from conductor.presentation.tickets import (
    TicketAppealButton,
    TicketApproveButton,
    TicketApproveReqButton,
    TicketCard,
    TicketCloseButton,
    TicketFaqButton,
    TicketLogCard,
    TicketOpenButton,
    TicketOutcomeSelect,
    TicketOverturnButton,
    TicketReasonButton,
    TicketRenameButton,
    TicketTakeButton,
    TicketTranscriptButton,
    TicketUpholdButton,
    public_panel_view,
    ticket_anon_relay_view,
    ticket_card_view,
    ticket_closed_dm_view,
    ticket_confirm_view,
    ticket_hint_view,
    ticket_log_card_view,
    ticket_staff_relay_view,
)
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.moderation import ModerationService
from conductor.services.queue import ActionQueue
from conductor.services.reputation import ReputationService
from conductor.services.tickets import TicketService, _staff_target
from conductor.services.transcripts import collect_transcript


def test_open_limit_blocks_second_ticket() -> None:
    assert can_open_ticket(0, limit=1) is True
    assert can_open_ticket(1, limit=1) is False
    assert can_open_ticket(1, limit=2) is True


def test_next_ticket_number_starts_at_one() -> None:
    assert next_ticket_number(None) == 1
    assert next_ticket_number(7) == 8


def test_ticket_channel_name_fits_discord_limit() -> None:
    name = ticket_channel_name(1, "Жалоба", "я" * 200)
    assert len(name) <= 100
    assert name.startswith("1-")


def test_anonymous_channel_author_is_anon_slug() -> None:
    assert ticket_channel_author("Секрет", anonymous=True) == ANON_CHANNEL_AUTHOR
    assert ticket_channel_author("Секрет", anonymous=False) == "Секрет"
    assert "секрет" not in ticket_channel_name(3, "Анонимно", ticket_channel_author("Секрет", anonymous=True))


def test_staff_relay_skips_bot_slash_voice_and_empty() -> None:
    bot = SimpleNamespace(author=SimpleNamespace(bot=True), content="hi", attachments=(), flags=None)
    assert is_staff_relay_skip(bot) is True
    slash = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        content="/tickets",
        attachments=(),
        flags=None,
        interaction=None,
        interaction_metadata=None,
    )
    assert is_staff_relay_skip(slash) is True
    voice = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        content="",
        attachments=(),
        flags=SimpleNamespace(voice=True),
        interaction=None,
        interaction_metadata=None,
    )
    assert is_staff_relay_skip(voice) is True
    empty = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        content="   ",
        attachments=(),
        flags=None,
        interaction=None,
        interaction_metadata=None,
    )
    assert is_staff_relay_skip(empty) is True
    ok = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        content="текст",
        attachments=(),
        flags=None,
        interaction=None,
        interaction_metadata=None,
    )
    assert is_staff_relay_skip(ok) is False


def test_staff_relay_skip_does_not_read_deprecated_interaction() -> None:
    class _Msg:
        author = SimpleNamespace(bot=False)
        content = "текст"
        attachments = ()
        flags = None
        interaction_metadata = object()

        @property
        def interaction(self) -> object:
            warnings.warn("Message.interaction is deprecated", DeprecationWarning, stacklevel=2)
            return object()

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert is_staff_relay_skip(_Msg()) is True


def test_dm_fail_due_waits_ten_minutes() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert dm_fail_due(None, now) is True
    assert dm_fail_due(now, now + timedelta(minutes=9)) is False
    assert dm_fail_due(now, now + timedelta(minutes=10)) is True


@pytest.mark.parametrize(
    (
        "actor",
        "author",
        "claimed_by",
        "actor_rank",
        "claimed_rank",
        "close_any",
        "close_own",
        "author_ok",
        "closed",
        "expect",
    ),
    [
        (9, 1, None, Rank.NONE, Rank.NONE, False, False, False, False, CloseVerdict.STRANGER),
        (1, 1, None, Rank.NONE, Rank.NONE, False, False, False, False, CloseVerdict.AUTHOR_BLOCKED),
        (1, 1, None, Rank.NONE, Rank.NONE, False, False, True, False, CloseVerdict.OK),
        (2, 1, None, Rank.WATCHER, Rank.NONE, False, True, False, False, CloseVerdict.OK),
        (2, 1, 2, Rank.WATCHER, Rank.WATCHER, False, True, False, False, CloseVerdict.OK),
        (2, 1, 3, Rank.WATCHER, Rank.SENIOR, False, True, False, False, CloseVerdict.HIERARCHY),
        (2, 1, 4, Rank.WATCHER, Rank.WATCHER, False, True, False, False, CloseVerdict.HIERARCHY),
        (3, 1, 5, Rank.SENIOR, Rank.SPECIAL, False, True, False, False, CloseVerdict.HIERARCHY),
        (3, 1, 5, Rank.SENIOR, Rank.SPECIAL, True, True, False, False, CloseVerdict.OK),
        (4, 1, 5, Rank.CHIEF, Rank.SPECIAL, True, True, False, False, CloseVerdict.OK),
        (5, 1, 6, Rank.SPECIAL, Rank.SPECIAL, True, True, False, False, CloseVerdict.OK),
        (2, 1, None, Rank.WATCHER, Rank.NONE, False, True, False, True, CloseVerdict.ALREADY),
        (9, 1, None, Rank.NONE, Rank.NONE, False, False, False, True, CloseVerdict.ALREADY),
    ],
)
def test_close_matrix_by_rank(
    actor: int,
    author: int,
    claimed_by: int | None,
    actor_rank: Rank,
    claimed_rank: Rank,
    close_any: bool,
    close_own: bool,
    author_ok: bool,
    closed: bool,
    expect: CloseVerdict,
) -> None:
    assert (
        may_close(
            actor_id=actor,
            author_id=author,
            claimed_by=claimed_by,
            actor_rank=actor_rank,
            claimed_rank=claimed_rank,
            has_close_any=close_any,
            has_close_own=close_own,
            author_may_close=author_ok,
            closed=closed,
        )
        is expect
    )


def test_public_panel_and_card_keep_persistent_ids() -> None:
    view = public_panel_view(
        ((11, "Жалоба"), (12, "Разбан"), (13, "Вопрос"), (14, "Баг"), (15, "Другое")),
        open_count=3,
        limit=1,
    )
    text = collected_text(view)
    assert "Обращение к следящим" in text
    assert "один открытый тикет на человека" in text
    ids = _custom_ids(view)
    assert "c:tk:open:11" in ids
    assert "c:tk:open:15" in ids
    parsed = parse_custom_id("c:tk:open:11")
    assert parsed is not None
    assert parsed.kind == "tk"
    card = ticket_card_view(TicketCard(ticket_id=8, number=124, category="Жалоба", author_id=1, claimed_by=None))
    card_ids = _custom_ids(card)
    assert "c:tk:take:8" in card_ids
    assert "c:tk:drop:8" in card_ids
    assert "c:tk:note:8" in card_ids
    assert "c:tk:ping:8" in card_ids
    assert "c:tk:prio:8" in card_ids
    assert "c:tk:xfer:8" in card_ids
    assert "c:tk:add:8" in card_ids
    assert "c:tk:rem:8" in card_ids
    assert "c:tk:close:8" in card_ids
    assert "c:tk:rename:8" in card_ids
    confirm_ids = _custom_ids(ticket_confirm_view(8))
    assert "c:tk:yes:8" in confirm_ids
    assert "c:tk:no:8" in confirm_ids
    staff_confirm = _custom_ids(ticket_confirm_view(8, outcomes=(Outcome.FIXED, Outcome.DUPLICATE)))
    assert "c:tk:outc:8" in staff_confirm
    assert "c:tk:yes:8" not in staff_confirm
    assert TicketOutcomeSelect in dynamic_items()
    assert TicketOpenButton in dynamic_items()
    assert TicketTakeButton in dynamic_items()
    assert TicketCloseButton in dynamic_items()
    assert TicketRenameButton in dynamic_items()
    assert TicketApproveReqButton in dynamic_items()
    assert TicketApproveButton in dynamic_items()
    assert TicketAppealButton in dynamic_items()
    assert TicketUpholdButton in dynamic_items()
    assert TicketOverturnButton in dynamic_items()
    assert TicketReasonButton in dynamic_items()
    assert TicketTranscriptButton in dynamic_items()
    log_card = ticket_log_card_view(
        TicketLogCard(
            ticket_id=8,
            number=124,
            category="Жалоба",
            author_id=1,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            state=TicketState.CLOSED,
            closed_by=7,
            closed_at=datetime(2026, 1, 2, tzinfo=UTC),
            outcome=Outcome.RESOLVED,
            close_reason="готово",
            rating=4,
        )
    )
    log_ids = _custom_ids(log_card)
    assert "c:tk:reason:8" in log_ids
    assert "c:tk:transcript:8" in log_ids
    log_text = collected_text(log_card)
    assert "Тикет #124" in log_text
    assert "★★★★☆" in log_text
    assert "готово" in log_text
    claimed_app = ticket_card_view(
        TicketCard(
            ticket_id=9,
            number=1,
            category="Заявка: следящий",
            author_id=1,
            claimed_by=7,
            show_approve_req=True,
            show_reject=True,
        )
    )
    app_ids = _custom_ids(claimed_app)
    assert "c:tk:approve_req:9" in app_ids
    assert "c:tk:reject:9" in app_ids
    pending = ticket_card_view(
        TicketCard(
            ticket_id=9,
            number=1,
            category="Заявка: следящий",
            author_id=1,
            claimed_by=7,
            state=TicketState.PENDING_APPROVAL,
            topic="moderator",
            show_approve=True,
            show_reject=True,
        )
    )
    pending_ids = _custom_ids(pending)
    pending_text = collected_text(pending)
    assert "c:tk:approve:9" in pending_ids
    assert "c:tk:reject:9" in pending_ids
    assert "ждёт решения специального следящего" in pending_text
    assert "/watchers appoint" in pending_text
    appeal = ticket_card_view(
        TicketCard(
            ticket_id=9,
            number=1,
            category="Заявка: следящий",
            author_id=1,
            claimed_by=7,
            state=TicketState.APPEAL_CHIEF,
            show_review=True,
            outcome=Outcome.REJECTED,
        )
    )
    appeal_ids = _custom_ids(appeal)
    assert "c:tk:uphold:9" in appeal_ids
    assert "c:tk:overturn:9" in appeal_ids
    closed_appeal = ticket_card_view(
        TicketCard(
            ticket_id=9,
            number=1,
            category="Заявка: следящий",
            author_id=1,
            claimed_by=7,
            closed=True,
            outcome=Outcome.REJECTED,
            show_appeal=True,
            appeal_owner=True,
        )
    )
    closed_ids = _custom_ids(closed_appeal)
    assert "c:tk:appeal:9" in closed_ids
    labels = {getattr(item, "label", None) for item in (getattr(closed_appeal, "walk_children", lambda: ())() or ())}
    assert "Обжаловать у владельца сервера" in labels
    hint = ticket_hint_view(11, title="Жалоба", hint="Сначала напишите в #помощь")
    hint_ids = _custom_ids(hint)
    assert "c:tk:faq:11" in hint_ids
    assert "c:tk:need:11" in hint_ids
    assert TicketFaqButton in dynamic_items()
    card_with = ticket_card_view(
        TicketCard(
            ticket_id=8,
            number=124,
            category="Жалоба",
            author_id=1,
            claimed_by=None,
            answers=(("Что случилось?", "выгнали из войса"),),
        )
    )
    assert "выгнали из войса" in collected_text(card_with)
    owner = 987654321098765432
    hidden = TicketCard(
        ticket_id=4,
        number=9,
        category="Анонимно",
        author_id=owner,
        claimed_by=None,
        anonymous=True,
        answers=(("Суть", "не видно кто"),),
        closed=True,
        outcome=Outcome.RESOLVED,
        show_appeal=True,
        state=TicketState.CLOSED,
    )
    rendered = "\n".join(
        (
            collected_text(ticket_card_view(hidden)),
            collected_text(ticket_closed_dm_view(hidden)),
            collected_text(ticket_anon_relay_view("секретный текст")),
            collected_text(ticket_staff_relay_view("watcher", "ответ")),
        )
    )
    assert str(owner) not in rendered
    assert f"<@{owner}>" not in rendered
    assert copy.TICKET_ANON_NAME in rendered
    assert "c:tk:r1:4" in _custom_ids(ticket_closed_dm_view(hidden))
    assert "c:tk:r1:4" not in _custom_ids(ticket_card_view(hidden))
    assert _staff_target(SimpleNamespace(anonymous=True, author_id=owner)) is None  # type: ignore[arg-type]
    hidden_log = collected_text(
        ticket_log_card_view(
            TicketLogCard(
                ticket_id=4,
                number=9,
                category="Анонимно",
                author_id=owner,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                state=TicketState.CLOSED,
                closed_by=7,
                anonymous=True,
            )
        )
    )
    assert str(owner) not in hidden_log
    assert f"<@{owner}>" not in hidden_log
    assert copy.TICKET_ANON_NAME in hidden_log


def test_may_edit_reason_closer_or_close_any_not_author() -> None:
    assert (
        may_edit_reason(
            actor_id=7,
            author_id=1,
            closed_by=7,
            has_close_any=False,
            state=TicketState.CLOSED,
        )
        is True
    )
    assert (
        may_edit_reason(
            actor_id=8,
            author_id=1,
            closed_by=7,
            has_close_any=True,
            state=TicketState.CLOSED,
        )
        is True
    )
    assert (
        may_edit_reason(
            actor_id=1,
            author_id=1,
            closed_by=1,
            has_close_any=True,
            state=TicketState.CLOSED,
        )
        is False
    )
    assert (
        may_edit_reason(
            actor_id=8,
            author_id=1,
            closed_by=7,
            has_close_any=False,
            state=TicketState.CLOSED,
        )
        is False
    )


def test_settings_roundtrip() -> None:
    parsed = parse_ticket_settings(
        '{"open_limit": 2, "author_may_close": true, "open_mode": "thread", "log_channel_id": 33}'
    )
    raw = dump_ticket_settings(parsed)
    again = parse_ticket_settings(raw)
    assert again.open_limit == 2
    assert again.author_may_close is True
    assert again.open_mode == "thread"
    assert again.log_channel_id == 33
    dumped = parse_ticket_settings(dump_ticket_settings(TicketSettings(log_channel_id=44)))
    assert dumped.log_channel_id == 44


def test_empty_form_opens_immediately() -> None:
    assert open_step(hint="", form=parse_ticket_form("{}")) is OpenStep.OPEN
    assert open_step(hint="", form=parse_ticket_form("")) is OpenStep.OPEN


def test_hint_beats_form_then_form_beats_open() -> None:
    form = collect_form_fields(["Что случилось?", "Когда?"])
    assert open_step(hint="Сначала #помощь", form=form) is OpenStep.HINT
    assert open_step(hint="", form=form) is OpenStep.FORM
    assert len(form.fields) == 2


def test_form_keeps_five_fields_and_drops_empty() -> None:
    form = collect_form_fields(["a", "", "b", "c", "d", "e", "f"])
    assert [field.prompt for field in form.fields] == ["a", "b", "c", "d", "e"]
    roundtrip = parse_ticket_form(dump_ticket_form(form))
    assert [field.prompt for field in roundtrip.fields] == ["a", "b", "c", "d", "e"]


def test_form_answers_go_to_transcript() -> None:
    raw = dump_form_answers([("Что случилось?", "выгнали из войса")])
    pairs = parse_form_answers(raw)
    assert pairs == (("Что случилось?", "выгнали из войса"),)
    assert "выгнали из войса" in format_form_transcript(pairs)


def test_escalate_allowed_uses_injected_clock() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    last = now - timedelta(minutes=9)
    assert escalate_allowed(None, now) is True
    assert escalate_allowed(last, now) is False
    assert escalate_allowed(last, now + timedelta(minutes=1)) is True


def test_second_rating_is_rejected() -> None:
    assert may_rate(actor_id=1, author_id=1, rating=None, closed=True) is RateVerdict.OK
    assert may_rate(actor_id=1, author_id=1, rating=4, closed=True) is RateVerdict.ALREADY
    assert may_rate(actor_id=2, author_id=1, rating=None, closed=True) is RateVerdict.STRANGER


def test_author_cannot_see_notes() -> None:
    assert may_see_notes(actor_id=1, author_id=1, has_notes_key=True) is False
    assert may_see_notes(actor_id=2, author_id=1, has_notes_key=True) is True
    assert may_see_notes(actor_id=2, author_id=1, has_notes_key=False) is False


def test_autoclose_warns_an_hour_before_deadline() -> None:
    last = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert autoclose_action(last, last + timedelta(hours=47), hours=48, warned_at=None) is AutocloseAction.WARN
    assert autoclose_action(last, last + timedelta(hours=47), hours=48, warned_at=last) is AutocloseAction.WAIT
    assert autoclose_action(last, last + timedelta(hours=48), hours=48, warned_at=last) is AutocloseAction.CLOSE


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


class _Aiter:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self._index = 0

    def __aiter__(self) -> _Aiter:
        return self

    async def __anext__(self) -> object:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


class _Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.deletes = 0
        self.sent: list[dict[str, object]] = []

    def history(self, **_kwargs: object) -> _Aiter:
        return _Aiter([])

    async def delete(self, **_kwargs: object) -> None:
        self.deletes += 1

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(id=99)


class _LogChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []
        self.messages: dict[int, SimpleNamespace] = {}

    async def send(self, **kwargs: object) -> SimpleNamespace:
        mid = 200 + len(self.sent)

        async def edit(**ekwargs: object) -> None:
            self.edits.append(ekwargs)

        message = SimpleNamespace(id=mid, edit=edit)
        self.messages[mid] = message
        self.sent.append(kwargs)
        return message

    async def fetch_message(self, message_id: int) -> SimpleNamespace:
        found = self.messages.get(int(message_id))
        if found is None:
            raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Message"})
        return found


async def test_second_close_does_not_delete_channel_twice(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        settings=SimpleNamespace(tickets_per_category=1),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
    )
    service = TicketService(bot)
    ticket_ch = _Channel(10)
    archive = _Channel(20)
    channels = {10: ticket_ch, 20: archive}
    guild = SimpleNamespace(id=1, get_channel=lambda cid: channels.get(cid), get_thread=lambda _cid: None)
    actor = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(
                guild_id=1,
                channel_id=3,
                settings=dump_ticket_settings(parse_ticket_settings('{"archive_channel_id": 20, "open_limit": 1}')),
                created_at=stamp,
                created_by=7,
            )
            session.add(panel)
            await session.flush()
            category = TicketCategory(panel_id=panel.id, guild_id=1, name="Жалоба", position=0)
            session.add(category)
            await session.flush()
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=1,
                    category_id=category.id,
                    panel_id=panel.id,
                    state=TicketState.OPEN,
                    claimed_rank=0,
                    channel_id=10,
                    created_at=stamp,
                )
            )
        first = await service.close_ticket(guild, 1, actor, outcome=Outcome.RESOLVED)  # type: ignore[arg-type]
        assert first is CloseVerdict.OK
        assert ticket_ch.deletes == 0
        assert archive.sent
        second = await service.close_ticket(guild, 1, actor)  # type: ignore[arg-type]
        assert second is CloseVerdict.ALREADY
        assert ticket_ch.deletes == 0
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
            assert row is not None
            assert row.state == TicketState.CLOSED
            assert row.closed_by == 7
    finally:
        await engine.dispose()


async def test_form_and_hint_roundtrip_and_empty_category_opens(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), collector=None, guard=None, outbound=None, roles=None)
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(
                guild_id=1,
                channel_id=3,
                settings="{}",
                created_at=stamp,
                created_by=7,
            )
            session.add(panel)
            await session.flush()
            session.add(TicketCategory(panel_id=panel.id, guild_id=1, name="Жалоба", position=0))
            session.add(TicketCategory(panel_id=panel.id, guild_id=1, name="Вопрос", position=1))
        assert await service.set_category_form(1, "Жалоба", ["Что случилось?", "Когда?"]) is True
        assert await service.set_category_hint(1, "Жалоба", "Сначала #помощь") is True
        step, hint, form, name = await service.open_preview(1, 1)
        assert name == "Жалоба"
        assert step is OpenStep.HINT
        assert "помощь" in hint
        assert [field.prompt for field in form.fields] == ["Что случилось?", "Когда?"]
        empty_step, _, empty_form, _ = await service.open_preview(1, 2)
        assert empty_step is OpenStep.OPEN
        assert empty_form.fields == ()
        shown = await service.category_help(1, "Жалоба")
        assert shown is not None
        await service.clear_category_help(1, "Жалоба")
        cleared = await service.open_preview(1, 1)
        assert cleared[0] is OpenStep.OPEN
    finally:
        await engine.dispose()


async def test_faq_counts_in_stats_and_answers_in_transcript(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bumps: list[object] = []

    def bump(guild_id: int, moment: datetime, kind: object, amount: int = 1, **_kwargs: object) -> None:
        bumps.append((guild_id, kind, amount))

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        collector=SimpleNamespace(bump=bump),
        guard=None,
        outbound=None,
        roles=None,
    )
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await service.mark_faq_resolved(1, now=stamp)
        assert bumps == [(1, EventKind.TICKET_FAQ, 1)]
        async with session_scope(sessions) as session:
            panel = TicketPanel(
                guild_id=1,
                channel_id=3,
                settings="{}",
                created_at=stamp,
                created_by=7,
            )
            session.add(panel)
            await session.flush()
            category = TicketCategory(panel_id=panel.id, guild_id=1, name="Жалоба", position=0)
            session.add(category)
            await session.flush()
            session.add(
                Ticket(
                    guild_id=1,
                    number=3,
                    owner_id=1,
                    category_id=category.id,
                    panel_id=panel.id,
                    state=TicketState.OPEN,
                    claimed_rank=0,
                    channel_id=10,
                    created_at=stamp,
                    form_answers=dump_form_answers([("Что случилось?", "выгнали из войса")]),
                )
            )
        ticket = await service._load(1, 1)
        assert ticket is not None
        payload, count = await service._transcript(_Channel(10), ticket)
        text = payload.decode("utf-8")
        assert "выгнали из войса" in text
        assert count == 0
    finally:
        await engine.dispose()


async def test_escalate_cooldown_on_fake_clock(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), guard=None, outbound=None, roles=None)
    service = TicketService(bot)
    ticket_ch = _Channel(10)
    guild = SimpleNamespace(
        id=1,
        get_channel=lambda cid: ticket_ch if cid == 10 else None,
        get_thread=lambda _cid: None,
        get_member=lambda _uid: None,
    )
    actor = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    stamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    try:
        await _seed_open_ticket(sessions, stamp)
        first = await service.escalate_ticket(guild, 1, actor, now=stamp)  # type: ignore[arg-type]
        assert first in {EscalateVerdict.OK, EscalateVerdict.EMPTY}
        second = await service.escalate_ticket(guild, 1, actor, now=stamp + timedelta(minutes=9))  # type: ignore[arg-type]
        assert second is EscalateVerdict.COOLDOWN
        third = await service.escalate_ticket(guild, 1, actor, now=stamp + timedelta(minutes=10))  # type: ignore[arg-type]
        assert third in {EscalateVerdict.OK, EscalateVerdict.EMPTY}
    finally:
        await engine.dispose()


async def test_second_rating_rejected_after_close(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        settings=SimpleNamespace(tickets_per_category=1),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
    )
    service = TicketService(bot)
    ticket_ch = _Channel(10)
    archive = _Channel(20)
    channels = {10: ticket_ch, 20: archive}
    guild = SimpleNamespace(id=1, get_channel=lambda cid: channels.get(cid), get_thread=lambda _cid: None)
    staff = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    author = SimpleNamespace(id=1, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_open_ticket(sessions, stamp, archive_id=20)
        closed = await service.close_ticket(guild, 1, staff, outcome=Outcome.RESOLVED)  # type: ignore[arg-type]
        assert closed is CloseVerdict.OK
        first = await service.rate_ticket(guild, 1, author, 5)  # type: ignore[arg-type]
        assert first is RateVerdict.OK
        second = await service.rate_ticket(guild, 1, author, 1)  # type: ignore[arg-type]
        assert second is RateVerdict.ALREADY
    finally:
        await engine.dispose()


async def test_author_cannot_load_staff_notes(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), guard=None, outbound=None, roles=None)
    service = TicketService(bot)
    guild = SimpleNamespace(id=1, get_channel=lambda _cid: None, get_thread=lambda _cid: None)
    author = SimpleNamespace(id=1, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    staff = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_open_ticket(sessions, stamp)
        assert await service.add_note(guild, 1, author, "секрет") is False  # type: ignore[arg-type]
        assert await service.list_notes(guild, 1, author) is None  # type: ignore[arg-type]
        assert await service.add_note(guild, 1, staff, "секрет") is True  # type: ignore[arg-type]
        notes = await service.list_notes(guild, 1, staff)  # type: ignore[arg-type]
        assert notes == [(7, "секрет")]
        assert await service.list_notes(guild, 1, author) is None  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def _seed_open_ticket(
    sessions: object,
    stamp: datetime,
    *,
    archive_id: int | None = None,
    log_id: int | None = None,
) -> None:
    settings = "{}"
    if archive_id is not None or log_id is not None:
        settings = dump_ticket_settings(
            TicketSettings(archive_channel_id=archive_id, log_channel_id=log_id, open_limit=1)
        )
    async with session_scope(sessions) as session:  # type: ignore[arg-type]
        panel = TicketPanel(
            guild_id=1,
            channel_id=3,
            settings=settings,
            created_at=stamp,
            created_by=7,
        )
        session.add(panel)
        await session.flush()
        category = TicketCategory(panel_id=panel.id, guild_id=1, name="Жалоба", position=0)
        session.add(category)
        await session.flush()
        session.add(
            Ticket(
                guild_id=1,
                number=1,
                owner_id=1,
                category_id=category.id,
                panel_id=panel.id,
                state=TicketState.OPEN,
                claimed_rank=0,
                channel_id=10,
                created_at=stamp,
                last_activity_at=stamp,
            )
        )


async def test_panel_is_live_requires_discord_message(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = TicketService(SimpleNamespace(sessions=sessions, scheduler=None))
    guild = SimpleNamespace(id=1)
    channel = SimpleNamespace(id=3)
    try:
        assert await service.panel_is_live(guild, channel) is False  # type: ignore[arg-type]
        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        async with session_scope(sessions) as session:
            session.add(
                TicketPanel(
                    guild_id=1,
                    channel_id=3,
                    message_id=88,
                    settings="{}",
                    created_at=stamp,
                    created_by=7,
                )
            )
        missing = SimpleNamespace(
            id=3,
            fetch_message=AsyncMock(side_effect=discord.HTTPException(MagicMock(status=404), "gone")),
        )
        assert await service.panel_is_live(guild, missing) is False  # type: ignore[arg-type]
        live = SimpleNamespace(id=3, fetch_message=AsyncMock(return_value=SimpleNamespace(id=88)))
        assert await service.panel_is_live(guild, live) is True  # type: ignore[arg-type]
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("kind", "rank", "expect"),
    [
        (CategoryKind.GENERAL, Rank.SENIOR, True),
        (CategoryKind.GENERAL, Rank.WATCHER, True),
        (CategoryKind.BUG, Rank.SENIOR, False),
        (CategoryKind.BUG, Rank.SPECIAL, True),
        (CategoryKind.SUGGESTION, Rank.WATCHER, False),
        (CategoryKind.SUGGESTION, Rank.SPECIAL, True),
        (CategoryKind.APPEAL, Rank.SENIOR, False),
        (CategoryKind.APPEAL, Rank.CHIEF, True),
        (CategoryKind.APPEAL, Rank.SPECIAL, True),
        (CategoryKind.COMPLAINT, Rank.WATCHER, True),
        (CategoryKind.APPLICATION, Rank.SENIOR, True),
    ],
)
def test_visible_rank_matrix(kind: CategoryKind, rank: Rank, expect: bool) -> None:
    visible = visible_rank_set(None, kind)
    assert rank_may_see(rank, visible) is expect
    assert rank_may_see(Rank.SENIOR, visible, is_administrator=True) is True


def test_allowed_outcomes_by_kind() -> None:
    assert Outcome.FIXED in allowed_outcomes(CategoryKind.BUG)
    assert Outcome.RESOLVED not in allowed_outcomes(CategoryKind.BUG)
    assert Outcome.SANCTIONED in allowed_outcomes(CategoryKind.COMPLAINT)
    assert Outcome.ACCEPTED in allowed_outcomes(CategoryKind.APPLICATION)
    assert Outcome.REJECTED in allowed_outcomes(CategoryKind.APPEAL)
    assert Outcome.RESOLVED in allowed_outcomes(CategoryKind.GENERAL)
    assert Outcome.ACCEPTED in allowed_outcomes(CategoryKind.SUGGESTION)


def test_open_verdict_tenure_and_ladder() -> None:
    spec = CategorySpec(
        "Заявка: следящий",
        CategoryKind.APPLICATION,
        topic="moderator",
        min_tenure_days=30,
        min_ladder_step="regular",
    )
    both = open_verdict(CategoryKind.APPLICATION, 12, "guest", spec)
    assert both.verdict is OpenVerdict.GATES
    assert both.need_days == 30
    assert both.have_days == 12
    tenure = open_verdict(CategoryKind.APPLICATION, 12, "regular", spec)
    assert tenure.verdict is OpenVerdict.TENURE
    ladder = open_verdict(CategoryKind.APPLICATION, 40, "guest", spec)
    assert ladder.verdict is OpenVerdict.LADDER
    ok = open_verdict(CategoryKind.APPLICATION, 40, "regular", spec)
    assert ok.verdict is OpenVerdict.OK
    hidden = open_verdict(CategoryKind.APPEAL, 40, "regular", spec)
    assert hidden.verdict is OpenVerdict.HIDDEN


def test_default_categories_are_eight_visible_kinds() -> None:
    assert len(DEFAULT_CATEGORIES) == 8
    assert DEFAULT_CATEGORIES[0] == "Жалоба"
    kinds = [spec.kind for spec in DEFAULT_CATEGORY_SPECS]
    assert kinds.count(CategoryKind.APPLICATION) == 3
    assert kinds.count(CategoryKind.APPEAL) == 1
    hidden = [spec for spec in DEFAULT_CATEGORY_SPECS if spec.hidden]
    assert hidden == [spec for spec in DEFAULT_CATEGORY_SPECS if spec.kind is CategoryKind.APPEAL]
    assert Rank.SPECIAL in default_visible_ranks(CategoryKind.BUG)
    assert Rank.SENIOR not in default_visible_ranks(CategoryKind.BUG)
    assert Rank.CHIEF in default_visible_ranks(CategoryKind.APPEAL)
    assert Rank.WATCHER not in default_visible_ranks(CategoryKind.APPEAL)


async def test_staff_close_without_outcome_is_impossible(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        settings=SimpleNamespace(tickets_per_category=1),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
    )
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    actor = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    guild = SimpleNamespace(id=1, get_channel=lambda _cid: None, get_thread=lambda _cid: None)
    try:
        await _seed_open_ticket(sessions, stamp)
        verdict = await service.close_ticket(guild, 1, actor)  # type: ignore[arg-type]
        assert verdict is CloseVerdict.NEED_OUTCOME
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
            assert row is not None
            assert row.state == TicketState.OPEN
            assert row.outcome is None
    finally:
        await engine.dispose()


async def test_ticket_closed_emits_once_including_autoclose(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    events = DomainEvents()
    received: list[TicketClosed] = []
    events.subscribe(TicketClosed, received.append)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        settings=SimpleNamespace(tickets_per_category=1),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
        events=events,
    )
    service = TicketService(bot)
    ticket_ch = _Channel(10)
    archive = _Channel(20)
    channels = {10: ticket_ch, 20: archive}
    guild = SimpleNamespace(id=1, get_channel=lambda cid: channels.get(cid), get_thread=lambda _cid: None)
    actor = SimpleNamespace(id=0, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_open_ticket(sessions, stamp, archive_id=20)
        first = await service.close_ticket(guild, 1, actor, system=True)  # type: ignore[arg-type]
        assert first is CloseVerdict.OK
        assert len(received) == 1
        assert received[0].outcome == Outcome.NO_ACTION
        assert received[0].ticket_id == 1
        second = await service.close_ticket(guild, 1, actor, system=True)  # type: ignore[arg-type]
        assert second is CloseVerdict.ALREADY
        assert len(received) == 1
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
            assert row is not None
            assert row.outcome == Outcome.NO_ACTION
        assert archive.sent
        text = collected_text(archive.sent[0]["view"])
        assert "без действия" in text
    finally:
        await engine.dispose()


async def test_migration_backfills_general_on_existing_categories(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine, "0052")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ticket_panels (guild_id, channel_id, settings, created_at, created_by) "
                    "VALUES (1, 3, '{}', '2026-01-01', 7)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO ticket_categories (panel_id, guild_id, name, position, hint, form_json) "
                    "VALUES (1, 1, 'Старая', 0, '', '{}')"
                )
            )
        await apply_migrations(engine, "0053")
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT kind, visible_ranks FROM ticket_categories WHERE name='Старая'"))
            ).one()
        assert row[0] == "general"
        assert "watcher" in row[1]
        assert "senior" in row[1]
        assert "special" in row[1]
    finally:
        await engine.dispose()


async def test_seed_categories_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = TicketService(SimpleNamespace(sessions=sessions, scheduler=None))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            await session.flush()
            await service._seed_categories(session, panel)
            await service._seed_categories(session, panel)
            names = (
                await session.scalars(
                    select(TicketCategory.name)
                    .where(TicketCategory.panel_id == panel.id)
                    .order_by(TicketCategory.position)
                )
            ).all()
            kinds = {
                name: kind
                for name, kind in (
                    await session.execute(
                        select(TicketCategory.name, TicketCategory.kind).where(TicketCategory.panel_id == panel.id)
                    )
                )
            }
        assert list(names) == [spec.name for spec in DEFAULT_CATEGORY_SPECS]
        assert kinds["Баг сервера"] == CategoryKind.BUG
        assert kinds["Жалоба"] == CategoryKind.COMPLAINT
        assert kinds["Обжалование"] == CategoryKind.APPEAL
        async with session_scope(sessions) as session:
            bug = await session.scalar(select(TicketCategory).where(TicketCategory.name == "Баг сервера"))
            assert bug is not None
            assert Rank.SENIOR not in visible_rank_set(bug.visible_ranks, bug.kind)
            assert Rank.SPECIAL in visible_rank_set(bug.visible_ranks, bug.kind)
            appeal = await session.scalar(select(TicketCategory).where(TicketCategory.name == "Обжалование"))
            assert appeal is not None
            assert appeal.hidden is True
            assert Rank.WATCHER not in visible_rank_set(appeal.visible_ranks, appeal.kind)
    finally:
        await engine.dispose()


async def test_senior_cannot_claim_bug_category(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), guard=None, outbound=None, roles=None)
    service = TicketService(bot)

    async def flags(_guild: object, _actor: object) -> tuple[Rank, bool, bool]:
        return Rank.SENIOR, True, True

    service._actor_flags = flags  # type: ignore[method-assign]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    actor = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    guild = SimpleNamespace(id=1, get_channel=lambda _cid: None, get_thread=lambda _cid: None)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            await session.flush()
            category = TicketCategory(
                panel_id=panel.id,
                guild_id=1,
                name="Баг сервера",
                position=0,
                kind=CategoryKind.BUG,
                visible_ranks='["special"]',
            )
            session.add(category)
            await session.flush()
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=1,
                    category_id=category.id,
                    panel_id=panel.id,
                    state=TicketState.OPEN,
                    claimed_rank=0,
                    channel_id=10,
                    created_at=stamp,
                )
            )
        claimed = await service.claim_ticket(guild, 1, actor)  # type: ignore[arg-type]
        assert claimed is ClaimVerdict.HIDDEN
        closed = await service.close_ticket(guild, 1, actor, outcome=Outcome.FIXED)  # type: ignore[arg-type]
        assert closed is CloseVerdict.HIDDEN
    finally:
        await engine.dispose()


def _custom_ids(view: object) -> set[str]:
    found: set[str] = set()
    walk = getattr(view, "walk_children", None)
    if not callable(walk):
        return found
    for item in walk():
        custom_id = getattr(item, "custom_id", None)
        if isinstance(custom_id, str):
            found.add(custom_id)
    return found


_FLOW_NOW = datetime(2026, 1, 10, tzinfo=UTC)
_FLOW_CLOSED = datetime(2026, 1, 8, tzinfo=UTC)


def _flow(
    *,
    state: str = TicketState.CLAIMED,
    action: str = FlowAction.APPROVE_REQ,
    role: str = FlowRole.CLAIMER,
    kind: str = CategoryKind.APPLICATION,
    outcome: str | None = None,
    now: datetime = _FLOW_NOW,
    closed_at: datetime | None = None,
    closer_is_actor: bool = False,
    used_levels: frozenset[str] = frozenset(),
):
    return transition(
        state,
        action,
        role,
        kind,
        outcome,
        now,
        closed_at,
        closer_is_actor=closer_is_actor,
        used_levels=used_levels,
    )


def test_transition_allows_every_legal_edge() -> None:
    pending = _flow()
    assert pending.ok is True
    assert pending.state == TicketState.PENDING_APPROVAL
    assert _flow(role=FlowRole.CLOSE_ANY).ok is True
    approved = _flow(state=TicketState.PENDING_APPROVAL, action=FlowAction.APPROVE, role=FlowRole.SPECIAL)
    assert approved.ok is True
    assert approved.state == TicketState.CLOSED
    assert _flow(state=TicketState.PENDING_APPROVAL, action=FlowAction.REJECT, role=FlowRole.SPECIAL).ok is True
    staff_reject = _flow(action=FlowAction.REJECT)
    assert staff_reject.ok is True
    assert staff_reject.state == TicketState.CLOSED
    first = _flow(
        state=TicketState.CLOSED,
        action=FlowAction.APPEAL,
        role=FlowRole.AUTHOR,
        outcome=Outcome.REJECTED,
        closed_at=_FLOW_CLOSED,
    )
    assert first.ok is True
    assert first.state == TicketState.APPEAL_CHIEF
    complaint = _flow(
        state=TicketState.CLOSED,
        action=FlowAction.APPEAL,
        role=FlowRole.AUTHOR,
        kind=CategoryKind.COMPLAINT,
        outcome=Outcome.POINTLESS,
        closed_at=_FLOW_CLOSED,
    )
    assert complaint.state == TicketState.APPEAL_CHIEF
    owner_level = _flow(
        state=TicketState.CLOSED,
        action=FlowAction.APPEAL,
        role=FlowRole.AUTHOR,
        outcome=Outcome.REJECTED,
        closed_at=_FLOW_CLOSED,
        used_levels=frozenset({"chief"}),
    )
    assert owner_level.state == TicketState.APPEAL_OWNER
    skip_chief = _flow(
        state=TicketState.CLOSED,
        action=FlowAction.APPEAL,
        role=FlowRole.AUTHOR,
        kind=CategoryKind.APPEAL,
        outcome=Outcome.REJECTED,
        closed_at=_FLOW_CLOSED,
    )
    assert skip_chief.state == TicketState.APPEAL_OWNER
    chief_uphold = _flow(state=TicketState.APPEAL_CHIEF, action=FlowAction.UPHOLD, role=FlowRole.CHIEF)
    assert chief_uphold.state == TicketState.CLOSED
    assert _flow(state=TicketState.APPEAL_CHIEF, action=FlowAction.UPHOLD, role=FlowRole.SPECIAL).ok is True
    chief_overturn = _flow(state=TicketState.APPEAL_CHIEF, action=FlowAction.OVERTURN, role=FlowRole.CHIEF)
    assert chief_overturn.state == TicketState.OPEN
    owner_uphold = _flow(state=TicketState.APPEAL_OWNER, action=FlowAction.UPHOLD, role=FlowRole.OWNER)
    assert owner_uphold.state == TicketState.CLOSED
    owner_overturn = _flow(state=TicketState.APPEAL_OWNER, action=FlowAction.OVERTURN, role=FlowRole.OWNER)
    assert owner_overturn.state == TicketState.OPEN


def test_transition_denies_forbidden_actions() -> None:
    assert _flow(state=TicketState.OPEN).reason == "wrong_state"
    assert _flow(role=FlowRole.AUTHOR).reason == "wrong_role"
    assert _flow(role=FlowRole.OTHER).reason == "wrong_role"
    assert _flow(kind=CategoryKind.COMPLAINT).reason == "wrong_kind"
    watcher_approve = _flow(state=TicketState.PENDING_APPROVAL, action=FlowAction.APPROVE, role=FlowRole.OTHER)
    assert watcher_approve.reason == "wrong_role"
    claimer_pending = _flow(state=TicketState.PENDING_APPROVAL, action=FlowAction.REJECT, role=FlowRole.CLAIMER)
    assert claimer_pending.reason == "wrong_role"
    assert _flow(action=FlowAction.REJECT, kind=CategoryKind.GENERAL).reason == "wrong_kind"
    assert (
        _flow(
            state=TicketState.CLOSED,
            action=FlowAction.APPEAL,
            role=FlowRole.AUTHOR,
            outcome=Outcome.ACCEPTED,
            closed_at=_FLOW_CLOSED,
        ).reason
        == "wrong_outcome"
    )
    assert (
        _flow(
            state=TicketState.CLOSED,
            action=FlowAction.APPEAL,
            role=FlowRole.AUTHOR,
            kind=CategoryKind.BUG,
            outcome=Outcome.REJECTED,
            closed_at=_FLOW_CLOSED,
        ).reason
        == "wrong_kind"
    )
    assert (
        _flow(
            state=TicketState.CLOSED,
            action=FlowAction.APPEAL,
            role=FlowRole.OTHER,
            outcome=Outcome.REJECTED,
            closed_at=_FLOW_CLOSED,
        ).reason
        == "wrong_role"
    )
    assert (
        _flow(
            state=TicketState.APPEAL_CHIEF,
            action=FlowAction.UPHOLD,
            role=FlowRole.CHIEF,
            closer_is_actor=True,
        ).reason
        == "closer"
    )
    assert _flow(state=TicketState.APPEAL_CHIEF, action=FlowAction.UPHOLD, role=FlowRole.OWNER).reason == "wrong_role"
    assert _flow(state=TicketState.APPEAL_OWNER, action=FlowAction.UPHOLD, role=FlowRole.SPECIAL).reason == "wrong_role"
    assert (
        _flow(
            state=TicketState.APPEAL_OWNER,
            action=FlowAction.OVERTURN,
            role=FlowRole.OWNER,
            closer_is_actor=True,
        ).reason
        == "closer"
    )


def test_appeal_window_uses_injected_clock() -> None:
    closed_at = datetime(2026, 1, 1, tzinfo=UTC)
    on_time = closed_at + timedelta(days=7)
    late = closed_at + timedelta(days=7, seconds=1)
    assert (
        can_appeal(
            kind=CategoryKind.APPLICATION,
            outcome=Outcome.REJECTED,
            now=on_time,
            closed_at=closed_at,
        )
        is True
    )
    assert (
        can_appeal(
            kind=CategoryKind.APPLICATION,
            outcome=Outcome.REJECTED,
            now=late,
            closed_at=closed_at,
        )
        is False
    )
    assert (
        _flow(
            state=TicketState.CLOSED,
            action=FlowAction.APPEAL,
            role=FlowRole.AUTHOR,
            outcome=Outcome.REJECTED,
            now=late,
            closed_at=closed_at,
        ).reason
        == "too_late"
    )


def test_appeal_once_per_level() -> None:
    both = frozenset({"chief", "owner"})
    assert (
        can_appeal(
            kind=CategoryKind.APPLICATION,
            outcome=Outcome.REJECTED,
            now=_FLOW_NOW,
            closed_at=_FLOW_CLOSED,
            used_levels=both,
        )
        is False
    )
    assert (
        _flow(
            state=TicketState.CLOSED,
            action=FlowAction.APPEAL,
            role=FlowRole.AUTHOR,
            outcome=Outcome.REJECTED,
            closed_at=_FLOW_CLOSED,
            used_levels=both,
        ).reason
        == "already"
    )
    assert (
        can_appeal(
            kind=CategoryKind.APPEAL,
            outcome=Outcome.REJECTED,
            now=_FLOW_NOW,
            closed_at=_FLOW_CLOSED,
            used_levels=frozenset({"owner"}),
        )
        is False
    )


async def test_senior_sends_application_special_approves_watcher_cannot(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    events = DomainEvents()
    closed: list[TicketClosed] = []
    events.subscribe(TicketClosed, closed.append)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), guard=None, outbound=None, roles=None, events=events)
    service = TicketService(bot)
    senior = SimpleNamespace(id=20, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    special = SimpleNamespace(id=40, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    watcher = SimpleNamespace(id=10, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    guild = SimpleNamespace(
        id=1,
        owner_id=99,
        get_channel=lambda _cid: None,
        get_thread=lambda _cid: None,
        get_member=lambda _uid: None,
    )

    async def flags(_guild: object, actor: object) -> tuple[Rank, bool, bool]:
        rank = {20: Rank.SENIOR, 40: Rank.SPECIAL, 10: Rank.WATCHER}.get(getattr(actor, "id", 0), Rank.NONE)
        return rank, rank >= Rank.SENIOR, rank >= Rank.SENIOR

    async def has_key(_guild: object, actor: object, key: str) -> bool:
        actor_id = getattr(actor, "id", 0)
        if key == "tickets.approve":
            return actor_id == 40
        if key == "tickets.close_any":
            return actor_id in {20, 40}
        return False

    service._actor_flags = flags  # type: ignore[method-assign]
    service._has_key = has_key  # type: ignore[method-assign]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_application_ticket(sessions, stamp, claimed_by=20)
        sent = await service.request_approval(guild, 1, senior, now=stamp)  # type: ignore[arg-type]
        assert sent.ok is True
        assert sent.state == TicketState.PENDING_APPROVAL
        denied = await service.approve_ticket(guild, 1, watcher, now=stamp)  # type: ignore[arg-type]
        assert denied.ok is False
        assert denied.reason == "wrong_role"
        approved = await service.approve_ticket(guild, 1, special, now=stamp)  # type: ignore[arg-type]
        assert approved.ok is True
        assert len(closed) == 1
        assert closed[0].outcome == Outcome.ACCEPTED
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
            kinds = list((await session.scalars(select(TicketEvent.kind).where(TicketEvent.ticket_id == 1))).all())
        assert row is not None
        assert row.state == TicketState.CLOSED
        assert row.outcome == Outcome.ACCEPTED
        assert "approve_req" in kinds
        assert "approve" in kinds
    finally:
        await engine.dispose()


async def test_appeal_chain_closer_blocked_owner_is_guild_owner(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    events = DomainEvents()
    closed: list[TicketClosed] = []
    events.subscribe(TicketClosed, closed.append)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), guard=None, outbound=None, roles=None, events=events)
    service = TicketService(bot)
    author = SimpleNamespace(id=1, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    chief = SimpleNamespace(id=30, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    other_chief = SimpleNamespace(id=31, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    owner = SimpleNamespace(id=99, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    special = SimpleNamespace(id=40, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    guild = SimpleNamespace(
        id=1,
        owner_id=99,
        get_channel=lambda _cid: None,
        get_thread=lambda _cid: None,
        get_member=lambda _uid: None,
    )

    async def flags(_guild: object, actor: object) -> tuple[Rank, bool, bool]:
        rank = {30: Rank.CHIEF, 31: Rank.CHIEF, 40: Rank.SPECIAL, 99: Rank.NONE}.get(getattr(actor, "id", 0), Rank.NONE)
        return rank, rank >= Rank.SENIOR, rank >= Rank.SENIOR

    async def has_key(_guild: object, actor: object, key: str) -> bool:
        actor_id = getattr(actor, "id", 0)
        if key == "tickets.approve":
            return actor_id == 40
        if key == "tickets.close_any":
            return actor_id in {30, 31, 40}
        return False

    service._actor_flags = flags  # type: ignore[method-assign]
    service._has_key = has_key  # type: ignore[method-assign]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_application_ticket(
            sessions,
            stamp,
            claimed_by=30,
            state=TicketState.CLOSED,
            outcome=Outcome.REJECTED,
            closed_by=30,
        )
        appealed = await service.appeal_ticket(guild, 1, author, now=stamp)  # type: ignore[arg-type]
        assert appealed.ok is True
        assert appealed.state == TicketState.APPEAL_CHIEF
        blocked = await service.uphold_ticket(guild, 1, chief, now=stamp)  # type: ignore[arg-type]
        assert blocked.ok is False
        assert blocked.reason == "closer"
        kept = await service.uphold_ticket(guild, 1, other_chief, now=stamp)  # type: ignore[arg-type]
        assert kept.ok is True
        assert len(closed) == 0
        second = await service.appeal_ticket(guild, 1, author, now=stamp + timedelta(days=1))  # type: ignore[arg-type]
        assert second.state == TicketState.APPEAL_OWNER
        special_denied = await service.uphold_ticket(guild, 1, special, now=stamp)  # type: ignore[arg-type]
        assert special_denied.reason == "wrong_role"
        owner_kept = await service.uphold_ticket(guild, 1, owner, now=stamp)  # type: ignore[arg-type]
        assert owner_kept.ok is True
        third = await service.appeal_ticket(guild, 1, author, now=stamp + timedelta(days=2))  # type: ignore[arg-type]
        assert third.ok is False
        assert third.reason == "already"
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
            kinds = list((await session.scalars(select(TicketEvent.kind).where(TicketEvent.ticket_id == 1))).all())
        assert row is not None
        assert row.state == TicketState.CLOSED
        assert row.closed_by == 30
        assert row.outcome == Outcome.REJECTED
        assert kinds.count("appeal_chief") == 1
        assert kinds.count("appeal_owner") == 1
        assert kinds.count("uphold") == 2
    finally:
        await engine.dispose()


async def test_review_states_skip_autoclose_and_remind_after_48h(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = TicketService(SimpleNamespace(sessions=sessions, scheduler=None))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_application_ticket(sessions, stamp, claimed_by=20, state=TicketState.PENDING_APPROVAL)
        async with session_scope(sessions) as session:
            session.add(
                TicketEvent(
                    ticket_id=1,
                    guild_id=1,
                    kind="approve_req",
                    actor_id=20,
                    note="",
                    created_at=stamp,
                )
            )
        jobs = await service._lifecycle_jobs(stamp + timedelta(hours=47))
        assert all(job["action"] != AutocloseAction.CLOSE for job in jobs)
        assert all(job.get("action") != "remind" for job in jobs)
        later = await service._lifecycle_jobs(stamp + timedelta(hours=48))
        reminds = [job for job in later if job["action"] == "remind"]
        assert len(reminds) == 1
        assert reminds[0]["ticket_id"] == 1
    finally:
        await engine.dispose()


async def _seed_application_ticket(
    sessions: object,
    stamp: datetime,
    *,
    claimed_by: int | None = None,
    state: str = TicketState.CLAIMED,
    outcome: Outcome | None = None,
    closed_by: int | None = None,
) -> None:
    async with session_scope(sessions) as session:  # type: ignore[arg-type]
        panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
        session.add(panel)
        await session.flush()
        category = TicketCategory(
            panel_id=panel.id,
            guild_id=1,
            name="Заявка: следящий",
            position=0,
            kind=CategoryKind.APPLICATION,
            topic="moderator",
        )
        session.add(category)
        await session.flush()
        session.add(
            Ticket(
                guild_id=1,
                number=1,
                owner_id=1,
                category_id=category.id,
                panel_id=panel.id,
                state=state,
                claimed_by=claimed_by,
                claimed_rank=int(Rank.SENIOR),
                channel_id=10,
                created_at=stamp,
                last_activity_at=stamp,
                closed_at=stamp if state == TicketState.CLOSED else None,
                closed_by=closed_by,
                outcome=None if outcome is None else outcome,
            )
        )


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class _OpenChannel(_Channel):
    def __init__(self, channel_id: int) -> None:
        super().__init__(channel_id)
        self.name = ""
        self.overwrites: dict[object, object] = {}

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)
        message = SimpleNamespace(id=99)

        async def edit(**edit_kwargs: object) -> SimpleNamespace:
            self.sent.append(edit_kwargs)
            return message

        message.edit = edit
        return message


class _Member:
    def __init__(self, user_id: int, guild: object, *, fail_dm: bool = False) -> None:
        self.id = user_id
        self.guild = guild
        self.display_name = "SecretName"
        self.roles = ()
        self.joined_at = None
        self.dms: list[dict[str, object]] = []
        self.fail_dm = fail_dm
        self.dm_channel = SimpleNamespace(id=555)
        self.guild_permissions = SimpleNamespace(administrator=False)

    async def send(self, **kwargs: object) -> SimpleNamespace:
        if self.fail_dm:
            raise discord.Forbidden(MagicMock(status=403), "blocked")
        self.dms.append(kwargs)
        return SimpleNamespace(id=1, channel=self.dm_channel)


def _message(
    *,
    author: object,
    content: str,
    guild: object | None = None,
    channel: object | None = None,
    attachments: tuple[object, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        author=author,
        content=content,
        guild=guild,
        channel=channel,
        attachments=attachments,
        interaction=None,
        interaction_metadata=None,
        flags=SimpleNamespace(voice=False),
    )


async def _seed_anonymous_ticket(
    sessions: object,
    stamp: datetime,
    *,
    owner_id: int = 1,
    archive_id: int | None = None,
    log_id: int | None = None,
    state: str = TicketState.OPEN,
) -> None:
    settings = "{}"
    if archive_id is not None or log_id is not None:
        settings = dump_ticket_settings(
            TicketSettings(archive_channel_id=archive_id, log_channel_id=log_id, open_limit=1)
        )
    async with session_scope(sessions) as session:  # type: ignore[arg-type]
        panel = TicketPanel(guild_id=1, channel_id=3, settings=settings, created_at=stamp, created_by=7)
        session.add(panel)
        await session.flush()
        category = TicketCategory(
            panel_id=panel.id,
            guild_id=1,
            name="Анонимно",
            position=0,
            kind=CategoryKind.ANONYMOUS,
        )
        session.add(category)
        await session.flush()
        session.add(
            Ticket(
                guild_id=1,
                number=1,
                owner_id=owner_id,
                category_id=category.id,
                panel_id=panel.id,
                state=state,
                claimed_rank=0,
                channel_id=10,
                created_at=stamp,
                last_activity_at=stamp,
                anonymous=True,
                dm_channel_id=555,
            )
        )


async def test_anonymous_open_forbidden_creates_no_row(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    created: list[_OpenChannel] = []

    async def create_text_channel(name: str, **kwargs: object) -> _OpenChannel:
        channel = _OpenChannel(100)
        channel.name = name
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        created.append(channel)
        return channel

    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=99),
        default_role=SimpleNamespace(id=2),
        get_channel=lambda _cid: None,
        get_role=lambda _rid: None,
        get_member=lambda _uid: None,
        create_text_channel=create_text_channel,
    )
    member = _Member(42, guild, fail_dm=True)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), guard=None, outbound=None, roles=None, motion=None)
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            await session.flush()
            session.add(
                TicketCategory(
                    panel_id=panel.id,
                    guild_id=1,
                    name="Анонимно",
                    position=0,
                    kind=CategoryKind.ANONYMOUS,
                )
            )
        result = await service.open_ticket(guild, member, 1)  # type: ignore[arg-type]
        assert result.verdict is OpenVerdict.DM_CLOSED
        assert created == []
        async with session_scope(sessions) as session:
            assert (await session.scalar(select(func.count()).select_from(Ticket))) == 0
    finally:
        await engine.dispose()


async def test_concurrent_opens_get_distinct_numbers_and_claim_is_atomic(tmp_path: Path, monkeypatch) -> None:
    """Two panel presses at once: both tickets exist with unique numbers and no orphan channel."""
    calls: list[tuple] = []
    monkeypatch.setattr("conductor.services.tickets.note_success", lambda *args, **kwargs: calls.append(args))
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    created: list[_OpenChannel] = []

    async def create_text_channel(name: str, **kwargs: object) -> _OpenChannel:
        await asyncio.sleep(0)  # yield inside the Discord call — where the race used to bite
        channel = _OpenChannel(100 + len(created))
        channel.name = name
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        created.append(channel)
        return channel

    members: dict[int, _Member] = {}
    guild = SimpleNamespace(
        id=1,
        me=object(),
        default_role=object(),
        get_channel=lambda cid: next((item for item in created if item.id == cid), None),
        get_role=lambda _rid: None,
        get_member=lambda uid: members.get(uid),
        create_text_channel=create_text_channel,
        owner_id=99,
    )
    for uid in (42, 43):
        members[uid] = _Member(uid, guild)
    staff_a = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    staff_b = SimpleNamespace(id=8, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
        get_guild=lambda gid: guild if gid == 1 else None,
    )
    service = TicketService(bot)

    async def flags(_guild: object, actor: object) -> tuple[Rank, bool, bool]:
        return Rank.WATCHER, True, True

    service._actor_flags = flags  # type: ignore[method-assign]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            await session.flush()
            session.add(TicketCategory(panel_id=panel.id, guild_id=1, name="Вопрос", position=0))
        first, second = await asyncio.gather(
            service.open_ticket(guild, members[42], 1),  # type: ignore[arg-type]
            service.open_ticket(guild, members[43], 1),  # type: ignore[arg-type]
        )
        assert first.verdict is OpenVerdict.OK
        assert second.verdict is OpenVerdict.OK
        async with session_scope(sessions) as session:
            rows = list((await session.scalars(select(Ticket).order_by(Ticket.number))).all())
        assert [row.number for row in rows] == [1, 2]
        assert len(created) == 2
        assert calls
        assert all(channel.deletes == 0 for channel in created)

        ticket_id = rows[0].id
        verdicts = await asyncio.gather(
            service.claim_ticket(guild, ticket_id, staff_a),  # type: ignore[arg-type]
            service.claim_ticket(guild, ticket_id, staff_b),  # type: ignore[arg-type]
        )
        assert sorted(verdicts, key=str) == sorted([ClaimVerdict.OK, ClaimVerdict.TAKEN], key=str)
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, ticket_id)
        assert row is not None
        winner = staff_a if verdicts[0] is ClaimVerdict.OK else staff_b
        assert row.claimed_by == winner.id

        # Message routing: the ticket channel is live, an unrelated channel costs no query.
        live = await service._live_channel_ids(1)
        assert {channel.id for channel in created} == set(live)
        await engine.dispose()
        assert 555 not in await service._live_channel_ids(1)
    finally:
        await engine.dispose()


async def test_expired_closed_ticket_is_swept_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    channel = _Channel(10)
    guild = SimpleNamespace(id=1, get_channel=lambda cid: channel if cid == 10 else None, get_thread=lambda _c: None)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        guard=None,
        outbound=None,
        roles=None,
        get_guild=lambda gid: guild if gid == 1 else None,
    )
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            await session.flush()
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=5,
                    category_id=1,
                    panel_id=panel.id,
                    state=TicketState.CLOSED,
                    channel_id=10,
                    created_at=stamp,
                    closed_at=stamp,
                    closed_by=7,
                )
            )
        later = stamp + timedelta(days=30)
        assert len(await service._lifecycle_jobs(later)) == 1
        await service.sweep_lifecycle(now=later)
        assert channel.deletes == 1
        assert await service._lifecycle_jobs(later + timedelta(minutes=1)) == []
        await service.sweep_lifecycle(now=later + timedelta(minutes=1))
        assert channel.deletes == 1
    finally:
        await engine.dispose()


async def test_anonymous_open_relays_and_hides_owner(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    created: list[_OpenChannel] = []

    async def create_text_channel(name: str, **kwargs: object) -> _OpenChannel:
        channel = _OpenChannel(100)
        channel.name = name
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        created.append(channel)
        return channel

    everyone = object()
    bot_member = object()
    guild = SimpleNamespace(
        id=1,
        me=bot_member,
        default_role=everyone,
        get_channel=lambda _cid: None,
        get_role=lambda _rid: None,
        get_member=lambda uid: member if uid == 42 else staff if uid == 7 else None,
        create_text_channel=create_text_channel,
        owner_id=99,
    )
    member = _Member(42, guild)
    staff = SimpleNamespace(
        id=7,
        roles=(),
        guild_permissions=SimpleNamespace(administrator=True),
        send=None,
    )
    archive = _Channel(20)
    channels = {20: archive}

    def get_channel(cid: int) -> object | None:
        if created and cid == created[0].id:
            return created[0]
        return channels.get(cid)

    guild.get_channel = get_channel
    clock = _Clock()
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
        queue=ActionQueue(lambda *_a, **_k: None, clock=clock),
        get_guild=lambda gid: guild if gid == 1 else None,
    )
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(
                guild_id=1,
                channel_id=3,
                settings=dump_ticket_settings(parse_ticket_settings('{"archive_channel_id": 20, "open_limit": 2}')),
                created_at=stamp,
                created_by=7,
            )
            session.add(panel)
            await session.flush()
            session.add(
                TicketCategory(
                    panel_id=panel.id,
                    guild_id=1,
                    name="Анонимно",
                    position=0,
                    kind=CategoryKind.ANONYMOUS,
                )
            )
        opened = await service.open_ticket(guild, member, 1)  # type: ignore[arg-type]
        assert opened.verdict is OpenVerdict.OK
        assert created
        dest = created[0]
        assert member not in dest.overwrites
        assert "anon" in dest.name
        assert "secretname" not in dest.name
        assert member.dms
        assert "без вашего имени" in collected_text(member.dms[0]["view"])  # type: ignore[arg-type]
        async with session_scope(sessions) as session:
            row = await session.scalar(select(Ticket))
        assert row is not None
        assert row.anonymous is True
        assert row.dm_channel_id == 555
        assert row.owner_id == 42
        card = ticket_card_view(await service._build_card(await service._load(1, row.id)))  # type: ignore[arg-type]
        card_text = collected_text(card)
        assert copy.TICKET_ANON_NAME in card_text
        assert "<@42>" not in card_text
        second = await service.open_ticket(guild, member, 1)  # type: ignore[arg-type]
        assert second.verdict is OpenVerdict.ANON_OPEN

        inbound = _message(author=member, content="из лички", guild=None)
        assert await service.relay_dm(inbound) is True
        relayed = [item for item in dest.sent if copy.TICKET_ANON_RELAY in collected_text(item["view"])]  # type: ignore[arg-type]
        assert relayed
        inbound_text = collected_text(relayed[0]["view"])  # type: ignore[arg-type]
        assert "из лички" in inbound_text
        assert "<@42>" not in inbound_text
        # Второе сообщение подряд доходит тоже — раньше дребезг его молча терял.
        assert await service.relay_dm(_message(author=member, content="сразу следом")) is True
        assert any("сразу следом" in collected_text(item["view"]) for item in dest.sent)  # type: ignore[arg-type]
        clock.advance(2.0)

        async def flags(_guild: object, actor: object) -> tuple[Rank, bool, bool]:
            return Rank.WATCHER, True, True

        service._actor_flags = flags  # type: ignore[method-assign]
        staff_msg = _message(author=staff, content="ответ следящего", guild=guild, channel=dest)
        assert await service.relay_staff(staff_msg) is True
        assert any("Следящий" in collected_text(item["view"]) for item in member.dms[1:])  # type: ignore[arg-type]
        assert all("42" not in collected_text(item["view"]) for item in member.dms)  # type: ignore[arg-type]
        assert await service.relay_staff(_message(author=staff, content="ещё", guild=guild, channel=dest)) is True
        assert any("ещё" in collected_text(item["view"]) for item in member.dms)  # type: ignore[arg-type]

        closed = await service.close_ticket(guild, row.id, staff, outcome=Outcome.RESOLVED)  # type: ignore[arg-type]
        assert closed is CloseVerdict.OK
        dm_closed = collected_text(member.dms[-1]["view"])  # type: ignore[arg-type]
        assert "42" not in dm_closed
        assert copy.ticket_outcome_label(Outcome.RESOLVED) in dm_closed
        archive_text = collected_text(archive.sent[0]["view"])  # type: ignore[arg-type]
        assert "<@42>" not in archive_text
        assert "SecretName" not in archive_text
        assert copy.TICKET_ANON_NAME in archive_text
    finally:
        await engine.dispose()


async def test_anonymous_transcript_and_staff_dm_blocked(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    owner = 424242424242
    ticket_ch = _Channel(10)
    stamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    history = [
        SimpleNamespace(
            author=SimpleNamespace(id=owner, display_name="SecretName"),
            content="из лички",
            created_at=stamp,
            attachments=(),
        ),
        SimpleNamespace(
            author=SimpleNamespace(id=7, display_name="Следящий"),
            content="ответ",
            created_at=stamp,
            attachments=(),
        ),
    ]
    ticket_ch.history = lambda **_k: _Aiter(history)  # type: ignore[method-assign]
    author = _Member(owner, SimpleNamespace(id=1), fail_dm=True)
    guild = SimpleNamespace(
        id=1,
        get_channel=lambda cid: ticket_ch if cid == 10 else None,
        get_thread=lambda _cid: None,
        get_member=lambda uid: author if uid == owner else staff if uid == 7 else None,
    )
    staff = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        guard=None,
        outbound=None,
        roles=None,
        get_guild=lambda gid: guild if gid == 1 else None,
    )
    service = TicketService(bot)

    async def flags(_guild: object, actor: object) -> tuple[Rank, bool, bool]:
        return Rank.WATCHER, True, True

    service._actor_flags = flags  # type: ignore[method-assign]
    try:
        await _seed_anonymous_ticket(sessions, stamp, owner_id=owner)
        ticket = await service._load(1, 1)
        assert ticket is not None
        payload, count = await service._transcript(ticket_ch, ticket)
        text = payload.decode("utf-8")
        assert count == 2
        assert str(owner) not in text
        assert "SecretName" not in text
        assert copy.TICKET_ANON_TRANSCRIPT in text
        assert "Следящий" in text
        history_text, _ = await collect_transcript(ticket_ch, anonymous_author_id=owner)
        assert str(owner) not in history_text

        staff_msg = _message(author=staff, content="не дойдёт", guild=guild, channel=ticket_ch)
        assert await service.relay_staff(staff_msg) is False
        warn = collected_text(ticket_ch.sent[0]["view"])  # type: ignore[arg-type]
        assert copy.TICKET_DM_BLOCKED in warn
        assert str(owner) not in warn
        async with session_scope(sessions) as session:
            kinds = list((await session.scalars(select(TicketEvent.kind))).all())
        assert kinds == ["dm_closed"]
        again = _message(author=staff, content="ещё раз", guild=guild, channel=ticket_ch)
        assert await service.relay_staff(again) is False
        assert len(ticket_ch.sent) == 1
    finally:
        await engine.dispose()


async def test_set_close_reason_rights(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(sessions=sessions, caches=CacheHub(), guard=None, outbound=None, roles=None)
    service = TicketService(bot)
    guild = SimpleNamespace(id=1, get_channel=lambda _cid: None, get_thread=lambda _cid: None)
    closer = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    other = SimpleNamespace(id=8, roles=(), guild_permissions=SimpleNamespace(administrator=False))
    author = SimpleNamespace(id=1, roles=(), guild_permissions=SimpleNamespace(administrator=False))

    async def flags(_guild: object, actor: object) -> tuple[Rank, bool, bool]:
        actor_id = int(getattr(actor, "id", 0) or 0)
        if actor_id == 7:
            return Rank.WATCHER, False, False
        if actor_id == 8:
            return Rank.CHIEF, True, True
        return Rank.NONE, False, False

    service._actor_flags = flags  # type: ignore[method-assign]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_open_ticket(sessions, stamp)
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
            assert row is not None
            row.state = TicketState.CLOSED
            row.closed_by = 7
            row.closed_at = stamp
            row.close_reason = "было"
        assert await service.set_close_reason(guild, 1, closer, "закрывший") is True
        assert await service.set_close_reason(guild, 1, other, "старший") is True
        assert await service.set_close_reason(guild, 1, author, "автор") is False
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
        assert row is not None
        assert row.close_reason == "старший"
    finally:
        await engine.dispose()


async def test_close_writes_transcript_and_log_card(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        settings=SimpleNamespace(tickets_per_category=1),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
    )
    service = TicketService(bot)
    ticket_ch = _Channel(10)
    archive = _Channel(20)
    log_ch = _LogChannel(30)
    channels = {10: ticket_ch, 20: archive, 30: log_ch}
    guild = SimpleNamespace(id=1, get_channel=lambda cid: channels.get(cid), get_thread=lambda _cid: None)
    closer = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    other = SimpleNamespace(id=8, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    author = SimpleNamespace(id=1, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_open_ticket(sessions, stamp, archive_id=20, log_id=30)
        closed = await service.close_ticket(guild, 1, closer, reason="исчерпано", outcome=Outcome.RESOLVED)
        assert closed is CloseVerdict.OK
        async with session_scope(sessions) as session:
            stored = await session.get(TicketTranscript, 1)
            row = await session.get(Ticket, 1)
        assert stored is not None
        assert row is not None
        assert row.log_message_id == 200
        assert log_ch.sent
        card = collected_text(log_ch.sent[0]["view"])  # type: ignore[arg-type]
        assert "Тикет #1" in card
        assert "исчерпано" in card
        assert "c:tk:reason:1" in _custom_ids(log_ch.sent[0]["view"])
        assert "c:tk:transcript:1" in _custom_ids(log_ch.sent[0]["view"])
        payload = await service.transcript_file(guild, 1, other)
        assert payload is not None
        assert payload[1] == copy.ticket_transcript_name(1)
        assert await service.set_close_reason(guild, 1, closer, "уточнили") is True
        assert await service.set_close_reason(guild, 1, other, "старший правил") is True
        assert await service.set_close_reason(guild, 1, author, "я автор") is False
        async with session_scope(sessions) as session:
            row = await session.get(Ticket, 1)
            kinds = list((await session.scalars(select(TicketEvent.kind))).all())
        assert row is not None
        assert row.close_reason == "старший правил"
        assert "reason" in kinds
        assert log_ch.edits
    finally:
        await engine.dispose()


async def test_anonymous_log_card_hides_owner(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        settings=SimpleNamespace(tickets_per_category=1),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
    )
    service = TicketService(bot)
    owner = 987654321098765432
    ticket_ch = _Channel(10)
    archive = _Channel(20)
    log_ch = _LogChannel(30)
    channels = {10: ticket_ch, 20: archive, 30: log_ch}
    guild = SimpleNamespace(id=1, get_channel=lambda cid: channels.get(cid), get_thread=lambda _cid: None)
    closer = SimpleNamespace(id=7, roles=(), guild_permissions=SimpleNamespace(administrator=True))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        await _seed_anonymous_ticket(sessions, stamp, owner_id=owner, archive_id=20, log_id=30)
        closed = await service.close_ticket(guild, 1, closer, outcome=Outcome.RESOLVED)
        assert closed is CloseVerdict.OK
        text = collected_text(log_ch.sent[0]["view"])  # type: ignore[arg-type]
        assert str(owner) not in text
        assert f"<@{owner}>" not in text
        assert copy.TICKET_ANON_NAME in text
    finally:
        await engine.dispose()


def test_explain_event_is_one_shot() -> None:
    assert has_explain(()) is False
    assert has_explain((TicketEventKind.REASON,)) is False
    assert has_explain((TicketEventKind.EXPLAIN,)) is True


async def test_case_appeal_overwrites_deny_punisher(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    created: list[_OpenChannel] = []

    async def create_text_channel(name: str, **kwargs: object) -> _OpenChannel:
        channel = _OpenChannel(100)
        channel.name = name
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        created.append(channel)
        return channel

    everyone = object()
    bot_member = object()
    guild = SimpleNamespace(
        id=1,
        me=bot_member,
        default_role=everyone,
        get_channel=lambda _cid: created[0] if created else None,
        get_role=lambda _rid: None,
        get_member=lambda uid: member if uid == 42 else punisher if uid == 7 else None,
        create_text_channel=create_text_channel,
        owner_id=99,
    )
    member = _Member(42, guild)
    punisher = _Member(7, guild)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
        get_guild=lambda _gid: guild,
        user=SimpleNamespace(id=99),
        fetch_user=None,
    )
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            session.add(
                ModCase(
                    guild_id=1,
                    number=4,
                    owner_id=42,
                    created_by=7,
                    actor_rank=1,
                    kind="warn",
                    reason="нарушение правил чата",
                    created_at=stamp,
                )
            )
        result = await service.open_case_appeal(
            guild,
            member,
            1,
            text="считаю наказание несправедливым полностью",
            now=stamp,
        )
        assert result.verdict is AppealVerdict.OK
        assert created
        assert result.channel_id == created[0].id
        denied_ids = [
            getattr(key, "id", None)
            for key, value in created[0].overwrites.items()
            if getattr(value, "view_channel", True) is False
        ]
        assert 7 in denied_ids
        again = await service.open_case_appeal(
            guild,
            member,
            1,
            text="считаю наказание несправедливым полностью",
            now=stamp,
        )
        assert again.verdict is AppealVerdict.ALREADY
        note = "наказание справедливо потому что повтор"
        first = await service.explain_case_appeal(result.ticket_id or 0, 7, note)
        assert first is ExplainVerdict.OK
        second = await service.explain_case_appeal(result.ticket_id or 0, 7, note)
        assert second is ExplainVerdict.ALREADY
        stranger = await service.explain_case_appeal(result.ticket_id or 0, 42, note)
        assert stranger is ExplainVerdict.DENIED
    finally:
        await engine.dispose()


def test_case_appeal_card_hides_generic_close() -> None:
    view = ticket_card_view(
        TicketCard(
            ticket_id=8,
            number=4,
            category="Обжалование",
            author_id=1,
            claimed_by=None,
            category_kind=CategoryKind.APPEAL,
        )
    )
    ids = _custom_ids(view)
    assert "c:tk:close:8" not in ids
    assert "c:tk:keep:8" in ids
    assert "c:tk:caserevoke:8" in ids
    assert copy.ticket_close_denied(CloseVerdict.CASE_APPEAL) == copy.TICKET_CASE_APPEAL


async def test_case_appeal_generic_close_blocked_accept_revokes(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    created: list[_OpenChannel] = []
    chief_role = 33

    async def create_text_channel(name: str, **kwargs: object) -> _OpenChannel:
        channel = _OpenChannel(100)
        channel.name = name
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        created.append(channel)
        return channel

    everyone = object()
    bot_member = object()
    guild = SimpleNamespace(
        id=1,
        me=bot_member,
        default_role=everyone,
        get_channel=lambda _cid: created[0] if created else None,
        get_role=lambda _rid: None,
        get_member=lambda uid: members.get(uid),
        create_text_channel=create_text_channel,
        owner_id=99,
    )
    member = _Member(42, guild)
    punisher = _Member(7, guild)
    chief = _Member(8, guild)
    chief.roles = (SimpleNamespace(id=chief_role),)
    members = {42: member, 7: punisher, 8: chief}
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
        get_guild=lambda _gid: guild,
        user=SimpleNamespace(id=99),
        fetch_user=None,
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: None),
    )
    bot.caches.permissions.set(
        (1, SNAPSHOT_CACHE_KEY),
        AccessSnapshot(
            rank_roles=empty_rank_roles() | {Rank.CHIEF: (chief_role,)},
            rules={},
            special_locked=True,
        ),
    )
    bot.reputation = ReputationService(bot)
    bot.moderation = ModerationService(bot)
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    hit_score = START - WARN_HIT
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            case = ModCase(
                guild_id=1,
                number=4,
                owner_id=42,
                created_by=7,
                actor_rank=int(Rank.WATCHER),
                kind="warn",
                reason="нарушение правил чата",
                created_at=stamp,
            )
            session.add(case)
            await session.flush()
            case_id = int(case.id)
            session.add(Reputation(guild_id=1, user_id=42, score=hit_score))
            session.add(
                ReputationEvent(
                    guild_id=1,
                    user_id=42,
                    case_id=case_id,
                    kind="warn",
                    delta=-WARN_HIT,
                    cut=0,
                    score_after=hit_score,
                    created_at=stamp,
                )
            )
        opened = await service.open_case_appeal(
            guild,
            member,
            case_id,
            text="считаю наказание несправедливым полностью",
            now=stamp,
        )
        assert opened.verdict is AppealVerdict.OK
        ticket_id = opened.ticket_id or 0
        card = await service.card_for(guild, ticket_id)
        assert card is not None
        ids = _custom_ids(card)
        assert f"c:tk:close:{ticket_id}" not in ids
        assert f"c:tk:keep:{ticket_id}" in ids
        assert f"c:tk:caserevoke:{ticket_id}" in ids
        blocked = await service.close_ticket(guild, ticket_id, chief, outcome=Outcome.ACCEPTED)
        assert blocked is CloseVerdict.CASE_APPEAL
        verdict = await service.close_verdict(guild, ticket_id, chief)
        assert verdict is CloseVerdict.CASE_APPEAL
        punisher_keep = await service.keep_case_appeal(
            guild, ticket_id, punisher, reason="наказание справедливо потому что повтор"
        )
        assert punisher_keep is CloseVerdict.STRANGER
        punisher_accept = await service.accept_case_appeal(
            guild, ticket_id, punisher, reason="ошибка следящего, снимаем наказание"
        )
        assert punisher_accept is ModVerdict.DENIED
        accepted = await service.accept_case_appeal(
            guild, ticket_id, chief, reason="ошибка следящего, снимаем наказание"
        )
        assert accepted is ModVerdict.OK
        async with session_scope(sessions) as session:
            case_row = await session.get(ModCase, case_id)
            ticket_row = await session.get(Ticket, ticket_id)
            score = await session.scalar(
                select(Reputation.score).where(Reputation.guild_id == 1, Reputation.user_id == 42)
            )
            kinds = list(
                (await session.scalars(select(ReputationEvent.kind).where(ReputationEvent.case_id == case_id))).all()
            )
        assert case_row is not None and case_row.revoked_at is not None
        assert ticket_row is not None
        assert ticket_row.state == TicketState.CLOSED
        assert ticket_row.outcome == Outcome.ACCEPTED
        assert score == START
        assert KIND_REVOKE in kinds
    finally:
        await engine.dispose()


async def test_anonymous_open_stays_on_shared_guild(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    home = SimpleNamespace(id=1, get_member=lambda uid: object() if uid == 42 else None)
    other = SimpleNamespace(id=2, get_member=lambda _uid: None)
    bot = SimpleNamespace(sessions=sessions, guilds=(home, other))
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            for guild_id in (1, 2):
                session.add(
                    Ticket(
                        guild_id=guild_id,
                        number=guild_id,
                        panel_id=1,
                        channel_id=100 + guild_id,
                        category_id=1,
                        owner_id=42,
                        state=TicketState.OPEN,
                        anonymous=True,
                        created_at=stamp,
                    )
                )
        found = await service._open_anonymous_for(42)
        assert found is not None
        assert found.guild_id == 1
        none = await service._open_anonymous_for(42, guild_id=2)
        assert none is None
    finally:
        await engine.dispose()
