from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from tests.test_profile import _card, _dossier, _noop

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import MemberWatch, Ticket, TicketCategory, TicketPanel
from conductor.db.session import session_scope
from conductor.domain.access import (
    MOD_BAN,
    MOD_HISTORY,
    MOD_KICK,
    MOD_MUTE,
    MOD_NOTE,
    MOD_REVOKE,
    MOD_TIMEOUT,
    MOD_UNMUTE,
    MOD_WARN,
    MOD_WATCH,
    AccessSnapshot,
    Rank,
    empty_rank_roles,
)
from conductor.domain.dossier import DossierAction, parse_watch_days, visible_actions
from conductor.domain.moderation import CaseKind, ModOutcome, ModVerdict
from conductor.domain.staff_feed import ACTION_WATCH_HIT, ACTION_WATCH_SET
from conductor.domain.tickets import TicketState, dump_ticket_settings, parse_ticket_settings
from conductor.presentation import copy
from conductor.presentation.dossier import TimeoutReasonModal, dossier_card, open_action_modal
from conductor.presentation.layout import collected_text
from conductor.presentation.moderation import deliver_case
from conductor.presentation.profile import TAB_REP, profile_card
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.dossier import DossierService
from conductor.services.ladder import LadderService
from conductor.services.moderation import ModerationService
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleApplyResult

WATCHER_ROLE = 11
CHIEF_ROLE = 33
REASON = "нарушение правил чата"
STAMP = datetime(2026, 9, 7, 8, tzinfo=UTC)
WATCHER_KEYS = {MOD_WARN, MOD_TIMEOUT, MOD_MUTE, MOD_UNMUTE, MOD_NOTE, MOD_WATCH, MOD_HISTORY}
CHIEF_KEYS = WATCHER_KEYS | {MOD_KICK, MOD_BAN}


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _ranked_snapshot() -> AccessSnapshot:
    return AccessSnapshot(
        rank_roles=empty_rank_roles() | {Rank.WATCHER: (WATCHER_ROLE,), Rank.CHIEF: (CHIEF_ROLE,)},
        rules={},
        special_locked=True,
    )


def _member(user_id: int, role_ids: tuple[int, ...] = (), *, admin: bool = False, bot: bool = False) -> SimpleNamespace:
    async def timeout(duration: object, *, reason: str | None = None) -> None:
        return None

    return SimpleNamespace(
        id=user_id,
        bot=bot,
        mention=f"<@{user_id}>",
        display_name=f"user-{user_id}",
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=admin),
        joined_at=STAMP - timedelta(days=40),
        timed_out_until=None,
        timeout=timeout,
    )


class _FakeRoles:
    def bind(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def apply(self, _claim: object) -> RoleApplyResult:
        return RoleApplyResult(ok=True)


def _bot(sessions) -> SimpleNamespace:
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        user=SimpleNamespace(id=99),
        outbound=None,
        roles=_FakeRoles(),
        log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
        config_by_guild={
            1: SimpleNamespace(
                user_log_channel_id=10,
                admin_log_channel_id=11,
                staff_feed_channel_id=12,
                timezone="UTC",
            )
        },
        queue=SimpleNamespace(schedule_later=lambda *_a, **_k: None, cancel_later=lambda *_a, **_k: None),
        scheduler=None,
        spawn=None,
        get_guild=lambda _gid: None,
        is_developer=lambda _uid: False,
    )
    bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), _ranked_snapshot())
    bot.moderation = ModerationService(bot)
    bot.reputation = ReputationService(bot)
    bot.ladder = LadderService(bot)
    bot.dossier = DossierService(bot)
    return bot


class _Response:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.modals: list[object] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_kwargs: object) -> None:
        self._done = True

    async def send_message(self, **kwargs: object) -> None:
        self.payloads.append(kwargs)
        self._done = True

    async def send_modal(self, modal: object) -> None:
        self.modals.append(modal)
        self._done = True


def test_parse_watch_days_rejects_out_of_range() -> None:
    assert parse_watch_days("7") == 7
    assert parse_watch_days("30") == 30
    assert parse_watch_days("0") is None
    assert parse_watch_days("31") is None
    assert parse_watch_days("нет") is None


def test_button_matrix_by_rank_and_can_punish() -> None:
    watcher_on_member = visible_actions(
        keys=WATCHER_KEYS, actor=Rank.WATCHER, target=Rank.NONE, self_view=False, target_is_bot=False
    )
    assert DossierAction.WARN in watcher_on_member
    assert DossierAction.TIMEOUT in watcher_on_member
    assert DossierAction.WATCH in watcher_on_member
    assert DossierAction.KICK not in watcher_on_member
    assert DossierAction.BAN not in watcher_on_member
    assert (
        visible_actions(
            keys=WATCHER_KEYS, actor=Rank.WATCHER, target=Rank.WATCHER, self_view=False, target_is_bot=False
        )
        == ()
    )
    chief = visible_actions(
        keys=CHIEF_KEYS, actor=Rank.CHIEF, target=Rank.WATCHER, self_view=False, target_is_bot=False
    )
    assert DossierAction.KICK in chief
    assert DossierAction.BAN in chief
    assert (
        visible_actions(keys=CHIEF_KEYS, actor=Rank.CHIEF, target=Rank.NONE, self_view=True, target_is_bot=False) == ()
    )
    assert (
        visible_actions(keys=CHIEF_KEYS, actor=Rank.CHIEF, target=Rank.NONE, self_view=False, target_is_bot=True) == ()
    )
    watching = visible_actions(
        keys=WATCHER_KEYS,
        actor=Rank.WATCHER,
        target=Rank.NONE,
        self_view=False,
        target_is_bot=False,
        watching=True,
    )
    assert DossierAction.WATCH not in watching
    assert DossierAction.WATCH_CLEAR in watching
    assert DossierAction.REVOKE not in chief
    with_revoke = visible_actions(
        keys=CHIEF_KEYS | {MOD_REVOKE},
        actor=Rank.CHIEF,
        target=Rank.SPECIAL,
        self_view=False,
        target_is_bot=False,
    )
    assert DossierAction.REVOKE in with_revoke
    assert DossierAction.WARN not in with_revoke


def test_dossier_card_hides_kick_for_watcher() -> None:
    from conductor.domain.dossier import DossierSnapshot, DossierTickets

    snap = DossierSnapshot(
        user_id=3,
        name="Ann",
        tenure_days=40,
        ladder_step="guest",
        rep_score=80,
        actions=(DossierAction.WARN, DossierAction.TIMEOUT, DossierAction.WATCH),
        tickets=DossierTickets(),
        self_roles=(copy.selfroles_member_line("Подписки", ["Опросы"]),),
    )
    view = dossier_card(snap, on_action=_noop, on_history=_noop)
    rendered = collected_text(view)
    assert copy.dossier_action_label(DossierAction.KICK.value) not in rendered
    assert copy.dossier_action_label(DossierAction.BAN.value) not in rendered
    assert copy.DOSSIER_HISTORY in rendered
    assert "Подписки" in rendered
    assert "Опросы" in rendered


def test_profile_staff_block_can_hold_dossier_button() -> None:
    view = profile_card(
        name="Ann",
        tenure_days=40,
        card=_card(),
        dossier=_dossier(),
        tab=TAB_REP,
        on_tab=_noop,
        on_dossier=_noop,
    )
    assert copy.DOSSIER_OPEN in collected_text(view)


async def test_timeout_modal_calls_apply_like_slash(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    called: list[dict[str, object]] = []

    async def apply(guild: object, actor: object, target: object, **kwargs: object) -> ModOutcome:
        called.append({"guild": guild, "actor": actor, "target": target, **kwargs})
        return ModOutcome(ModVerdict.OK)

    bot.moderation.apply = apply
    guild = SimpleNamespace(id=1, owner_id=1)
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    followups: list[dict[str, object]] = []

    async def followup_send(**kwargs: object) -> None:
        followups.append(kwargs)

    interaction = SimpleNamespace(
        guild=guild,
        user=actor,
        client=bot,
        response=_Response(),
        followup=SimpleNamespace(send=followup_send),
    )
    try:
        await open_action_modal(interaction, target, DossierAction.TIMEOUT)
        assert isinstance(interaction.response.modals[0], TimeoutReasonModal)
        interaction.response = _Response()
        await deliver_case(interaction, target, kind=CaseKind.TIMEOUT, reason=REASON, duration="10m")
        assert interaction.response.is_done(), "the sanction chain must be acknowledged before it runs"
        assert followups, "the outcome card goes through the followup after defer"
        assert len(called) == 1
        assert called[0]["kind"] is CaseKind.TIMEOUT
        assert called[0]["reason"] == REASON
        assert called[0]["duration_raw"] == "10m"
        assert called[0]["actor"] is actor
        assert called[0]["target"] is target
        interaction.response = _Response()
        await deliver_case(interaction, target, kind=CaseKind.WARN, reason=REASON)
        assert called[1]["kind"] is CaseKind.WARN
        assert called[1]["duration_raw"] is None
        assert called[1]["reason"] == REASON
    finally:
        await engine.dispose()


async def test_self_dossier_has_no_sanctions(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = SimpleNamespace(id=1)
    actor = _member(2, (WATCHER_ROLE,))
    try:
        snap = await bot.dossier.collect(guild, actor, actor, now=STAMP)
        assert snap.self_view is True
        assert snap.actions == ()
        text = collected_text(dossier_card(snap, on_action=_noop, on_history=_noop))
        assert copy.DOSSIER_WARN not in text
        assert copy.dossier_action_label(DossierAction.TIMEOUT.value) not in text
    finally:
        await engine.dispose()


async def test_one_active_watch_and_sweep(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    try:
        first = await bot.moderation.set_watch(
            1, 3, set_by=2, actor_rank=Rank.WATCHER, reason=REASON, days_raw="7", now=STAMP
        )
        second = await bot.moderation.set_watch(
            1, 3, set_by=2, actor_rank=Rank.WATCHER, reason="повторный повод слежки", days_raw="3", now=STAMP
        )
        assert first is not None
        assert second is not None
        async with session_scope(sessions) as session:
            rows = list((await session.scalars(select(MemberWatch))).all())
        assert len(rows) == 1
        assert rows[0].reason == "повторный повод слежки"
        assert await bot.moderation.watched_count(1, now=STAMP) == 1
        expired = await bot.moderation.set_watch(
            1, 4, set_by=2, actor_rank=Rank.WATCHER, reason=REASON, days_raw="1", now=STAMP - timedelta(days=2)
        )
        assert expired is not None
        cleared = await bot.moderation.sweep_watches(now=STAMP)
        assert cleared == 1
        assert await bot.moderation.active_watch(1, 4, now=STAMP) is None
        assert await bot.moderation.active_watch(1, 3, now=STAMP) is not None
        assert await bot.moderation.clear_watch(1, 3) is True
        assert await bot.moderation.active_watch(1, 3, now=STAMP) is None
    finally:
        await engine.dispose()


async def test_watch_hit_on_next_case(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    posts: list[str] = []

    async def capture(guild_id: int, **kwargs: object) -> int | None:
        posts.append(str(kwargs.get("action")))
        return 1

    bot.staff_feed = SimpleNamespace(post=capture)
    guild = SimpleNamespace(id=1, owner_id=1)
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    try:
        stored = await bot.moderation.set_watch(
            1, 3, set_by=2, actor_rank=Rank.WATCHER, reason=REASON, days_raw="7", now=STAMP
        )
        assert stored is not None
        assert ACTION_WATCH_SET in posts
        outcome = await bot.moderation.apply(
            guild, actor, target, kind=CaseKind.TIMEOUT, reason=REASON, duration_raw="10m", now=STAMP
        )
        assert outcome.verdict is ModVerdict.OK
        assert posts.count(ACTION_WATCH_HIT) == 1
    finally:
        await engine.dispose()


async def test_anonymous_tickets_are_hidden(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = SimpleNamespace(id=1)
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(
                guild_id=1,
                channel_id=3,
                settings=dump_ticket_settings(parse_ticket_settings("{}")),
                created_at=STAMP,
                created_by=2,
            )
            session.add(panel)
            await session.flush()
            visible = TicketCategory(panel_id=panel.id, guild_id=1, name="Жалоба", position=0)
            hidden = TicketCategory(panel_id=panel.id, guild_id=1, name="Анонимно", position=1)
            session.add_all([visible, hidden])
            await session.flush()
            session.add_all(
                [
                    Ticket(
                        guild_id=1,
                        number=1,
                        owner_id=3,
                        category_id=visible.id,
                        panel_id=panel.id,
                        state=TicketState.OPEN,
                        claimed_rank=0,
                        channel_id=10,
                        created_at=STAMP,
                    ),
                    Ticket(
                        guild_id=1,
                        number=2,
                        owner_id=3,
                        category_id=hidden.id,
                        panel_id=panel.id,
                        state=TicketState.OPEN,
                        claimed_rank=0,
                        channel_id=11,
                        created_at=STAMP,
                    ),
                ]
            )
        snap = await bot.dossier.collect(guild, actor, target, now=STAMP)
        assert snap.tickets.count == 1
        assert snap.tickets.categories == ("Жалоба",)
        text = collected_text(dossier_card(snap))
        assert "Жалоба" in text
        assert "Анонимно" not in text
    finally:
        await engine.dispose()
