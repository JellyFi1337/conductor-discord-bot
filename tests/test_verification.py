from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import ui
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import MemberRecord, ModCase, RoleChange, TicketPanel, VerificationSettings
from conductor.db.session import session_scope
from conductor.domain.access import AccessSnapshot, Rank, empty_rank_roles
from conductor.domain.moderation import CaseKind
from conductor.domain.pulse import PulseAlertRules
from conductor.domain.staff_feed import ACTION_JOIN_SPIKE
from conductor.domain.verification import (
    JOIN_SPIKE_FLOOR,
    TRAP_REASON,
    FunnelCounts,
    TrapVerdict,
    everyone_can_send,
    join_spike_due,
    trap_verdict,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import format_custom_id
from conductor.presentation.verification import rules_card
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.cache import CacheHub
from conductor.services.role_broker import RoleClaim, RoleSubsystem
from conductor.services.verification import VerificationService

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
GUILD_ID = 1
CHANNEL_ID = 55
ROLE_ID = 88
WATCHER_ROLE = 11


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _snapshot(*, watcher: bool = False) -> AccessSnapshot:
    ranks = empty_rank_roles()
    if watcher:
        ranks[Rank.WATCHER] = (WATCHER_ROLE,)
    return AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)


async def _show_config(_guild_id: int) -> SimpleNamespace:
    return SimpleNamespace(appeal_contact="tickets")


async def _alert_rules(_guild_id: int) -> PulseAlertRules:
    return PulseAlertRules()


async def _runtime(tmp_path: Path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    claims: list[RoleClaim] = []
    posts: list[dict[str, object]] = []
    submits: list[object] = []

    async def apply_role(claim: RoleClaim) -> SimpleNamespace:
        claims.append(claim)
        return SimpleNamespace(ok=True)

    async def staff_post(*_args: object, **kwargs: object) -> int:
        posts.append(kwargs)
        return 1

    apply_ban = AsyncMock()
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        roles=SimpleNamespace(bind=lambda *_args, **_kwargs: None, apply=apply_role),
        moderation=SimpleNamespace(apply=apply_ban),
        staff_feed=SimpleNamespace(post=staff_post),
        pulse=SimpleNamespace(alert_rules=_alert_rules),
        reputation=SimpleNamespace(show_config=_show_config),
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: submits.append(_kwargs)),
        config_by_guild={
            GUILD_ID: SimpleNamespace(admin_log_channel_id=99, user_log_channel_id=None),
        },
        serves=lambda _gid: True,
        outbound=None,
        user=SimpleNamespace(id=10),
    )
    bot.caches.permissions.set((GUILD_ID, SNAPSHOT_CACHE_KEY), _snapshot())
    return bot, engine, VerificationService(bot), claims, posts, submits, apply_ban


async def _seed_settings(sessions, *, channel_id: int | None = CHANNEL_ID, role_id: int | None = ROLE_ID) -> None:
    async with session_scope(sessions) as session:
        session.add(
            VerificationSettings(
                guild_id=GUILD_ID,
                channel_id=channel_id,
                verified_role_id=role_id,
            )
        )


def _author(
    user_id: int = 5,
    *,
    bot: bool = False,
    roles: tuple[int, ...] = (),
    administrator: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        bot=bot,
        roles=[SimpleNamespace(id=role_id) for role_id in roles],
        guild_permissions=SimpleNamespace(administrator=administrator),
        send=AsyncMock(),
    )


def _message(
    author: SimpleNamespace,
    *,
    webhook_id: int | None = None,
    channel_id: int = CHANNEL_ID,
    owner_id: int = 99,
) -> SimpleNamespace:
    return SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID, owner_id=owner_id, me=SimpleNamespace(id=10, roles=())),
        channel=SimpleNamespace(id=channel_id),
        author=author,
        webhook_id=webhook_id,
        delete=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "is_bot": True,
                "is_webhook": False,
                "rank": Rank.NONE,
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": True,
                "rank": Rank.NONE,
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.WATCHER,
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.SENIOR,
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.CHIEF,
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.SPECIAL,
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.WATCHER,
                "is_administrator": True,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.NONE,
                "is_administrator": True,
                "is_guild_owner": False,
            },
            TrapVerdict.BYPASS,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.NONE,
                "is_administrator": False,
                "is_guild_owner": True,
            },
            TrapVerdict.BYPASS,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": Rank.NONE,
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.BAN,
        ),
        (
            {
                "is_bot": False,
                "is_webhook": False,
                "rank": "watcher",
                "is_administrator": False,
                "is_guild_owner": False,
            },
            TrapVerdict.SKIP,
        ),
    ],
)
def test_trap_verdict_matrix(kwargs: dict[str, object], expected: TrapVerdict) -> None:
    assert trap_verdict(**kwargs) is expected  # type: ignore[arg-type]


def test_everyone_can_send_respects_deny_overwrite() -> None:
    assert everyone_can_send(overwrite=False, inherit=True) is False
    assert everyone_can_send(overwrite=True, inherit=False) is True
    assert everyone_can_send(overwrite=None, inherit=True) is True
    assert everyone_can_send(overwrite=None, inherit=False) is False


def test_join_spike_due_once_per_thirty_minutes() -> None:
    assert join_spike_due(joined=14, floor=JOIN_SPIKE_FLOOR, last=None, now=NOW) is False
    assert join_spike_due(joined=15, floor=JOIN_SPIKE_FLOOR, last=None, now=NOW) is True
    assert join_spike_due(joined=15, floor=JOIN_SPIKE_FLOOR, last=NOW, now=NOW + timedelta(minutes=10)) is False
    assert join_spike_due(joined=15, floor=JOIN_SPIKE_FLOOR, last=NOW, now=NOW + timedelta(minutes=30)) is True


def test_onboarding_without_channels_is_not_empty() -> None:
    lines = copy.onboarding_lines()
    assert lines
    assert copy.ONBOARDING_EMPTY in lines
    assert copy.ONBOARDING_HELP not in lines
    already = copy.onboarding_lines(already=True)
    assert copy.ONBOARDING_ALREADY in already
    assert copy.ONBOARDING_EMPTY in already


def test_rules_card_uses_guild_ref() -> None:
    view = rules_card(ref=7)
    buttons = [item for item in view.walk_children() if isinstance(item, ui.Button)]
    assert buttons
    assert buttons[0].custom_id == format_custom_id("vf", "ok", 7)
    assert buttons[0].label == copy.VERIFICATION_OK
    text = collected_text(view)
    assert copy.VERIFICATION_TITLE in text


async def test_grant_uses_broker_urgently_and_is_idempotent(tmp_path: Path) -> None:
    bot, engine, service, claims, _posts, _submits, _ban = await _runtime(tmp_path)
    try:
        await _seed_settings(bot.sessions)
        member = SimpleNamespace(id=5, roles=[])
        first = await service.grant(SimpleNamespace(id=GUILD_ID), member)
        assert claims
        claim = claims[0]
        assert claim.subsystem is RoleSubsystem.VERIFICATION
        assert claim.add == (ROLE_ID,)
        assert claim.urgent is True
        assert copy.ONBOARDING_ALREADY not in collected_text(first)
        assert copy.ONBOARDING_EMPTY in collected_text(first)
        member.roles = [SimpleNamespace(id=ROLE_ID)]
        second = await service.grant(SimpleNamespace(id=GUILD_ID), member)
        assert len(claims) == 1
        text = collected_text(second)
        assert copy.ONBOARDING_ALREADY in text
        assert copy.ONBOARDING_EMPTY in text
    finally:
        await engine.dispose()


async def test_onboarding_mentions_tickets_when_panel_exists(tmp_path: Path) -> None:
    bot, engine, service, _claims, _posts, _submits, _ban = await _runtime(tmp_path)
    try:
        await _seed_settings(bot.sessions)
        async with session_scope(bot.sessions) as session:
            session.add(
                TicketPanel(
                    guild_id=GUILD_ID,
                    channel_id=77,
                    message_id=1,
                    created_at=NOW,
                    created_by=2,
                )
            )
        member = SimpleNamespace(id=5, roles=[SimpleNamespace(id=ROLE_ID)])
        view = await service.grant(SimpleNamespace(id=GUILD_ID), member)
        text = collected_text(view)
        assert "<#77>" in text
        assert copy.ONBOARDING_EMPTY not in text
        assert copy.ONBOARDING_HELP in text
    finally:
        await engine.dispose()


async def test_inspect_bans_regular_member(tmp_path: Path) -> None:
    bot, engine, service, _claims, _posts, _submits, apply_ban = await _runtime(tmp_path)
    try:
        await _seed_settings(bot.sessions)
        await service.bind(GUILD_ID)
        author = _author()
        message = _message(author)
        assert await service.inspect(message) is True  # type: ignore[arg-type]
        message.delete.assert_awaited_once()
        author.send.assert_awaited()
        apply_ban.assert_awaited_once()
        kwargs = apply_ban.await_args.kwargs
        assert kwargs["kind"] is CaseKind.BAN
        assert kwargs["system"] is True
        assert kwargs["reason"] == TRAP_REASON
    finally:
        await engine.dispose()


async def test_inspect_logs_admin_bypass_once_per_day(tmp_path: Path) -> None:
    bot, engine, service, _claims, _posts, submits, apply_ban = await _runtime(tmp_path)
    try:
        await _seed_settings(bot.sessions)
        await service.bind(GUILD_ID)
        author = _author(administrator=True)
        message = _message(author)
        assert await service.inspect(message) is True  # type: ignore[arg-type]
        assert apply_ban.await_count == 0
        message.delete.assert_not_called()
        assert len(submits) == 1
        assert await service.inspect(message) is True  # type: ignore[arg-type]
        assert len(submits) == 1
    finally:
        await engine.dispose()


async def test_inspect_skips_staff_without_bypass_log(tmp_path: Path) -> None:
    bot, engine, service, _claims, _posts, submits, apply_ban = await _runtime(tmp_path)
    try:
        await _seed_settings(bot.sessions)
        await service.bind(GUILD_ID)
        bot.caches.permissions.set((GUILD_ID, SNAPSHOT_CACHE_KEY), _snapshot(watcher=True))
        author = _author(roles=(WATCHER_ROLE,), administrator=True)
        message = _message(author)
        assert await service.inspect(message) is True  # type: ignore[arg-type]
        assert apply_ban.await_count == 0
        assert submits == []
        message.delete.assert_not_called()
    finally:
        await engine.dispose()


async def test_grant_reports_broker_failure_once(tmp_path: Path) -> None:
    """Role above the bot: the newcomer gets a refusal, staff get one admin-log card, not a fake welcome."""
    bot, engine, service, claims, _posts, submits, _ban = await _runtime(tmp_path)

    async def failing_apply(claim: RoleClaim) -> SimpleNamespace:
        claims.append(claim)
        return SimpleNamespace(ok=False, dropped=(), error="роль выше бота")

    bot.roles = SimpleNamespace(bind=lambda *_args, **_kwargs: None, apply=failing_apply)
    try:
        await _seed_settings(bot.sessions)
        member = SimpleNamespace(id=5, roles=[])
        first = await service.grant(SimpleNamespace(id=GUILD_ID), member)
        assert copy.VERIFICATION_ROLE_FAILED in collected_text(first)
        assert copy.ONBOARDING_TITLE not in collected_text(first)
        assert len(submits) == 1
        await service.grant(SimpleNamespace(id=GUILD_ID), SimpleNamespace(id=6, roles=[]))
        assert len(submits) == 1, "one admin card per guild until the binding changes"
        await service.bind(GUILD_ID)
        await service.grant(SimpleNamespace(id=GUILD_ID), SimpleNamespace(id=7, roles=[]))
        assert len(submits) == 2
    finally:
        await engine.dispose()


async def test_inspect_ignores_system_messages(tmp_path: Path) -> None:
    """«X присоединился» arrives authored by the newcomer; it must never trigger the trap."""
    bot, engine, service, _claims, _posts, _submits, apply_ban = await _runtime(tmp_path)
    try:
        await _seed_settings(bot.sessions)
        await service.bind(GUILD_ID)
        message = _message(_author())
        message.type = discord.MessageType.new_member
        assert await service.inspect(message) is True  # type: ignore[arg-type]
        assert apply_ban.await_count == 0
        message.delete.assert_not_called()
    finally:
        await engine.dispose()


async def test_inspect_caches_missing_channel(tmp_path: Path) -> None:
    """A guild without verification must not hit the database on every message."""
    bot, engine, service, _claims, _posts, _submits, apply_ban = await _runtime(tmp_path)
    try:
        assert await service.inspect(_message(_author())) is False  # type: ignore[arg-type]
        await engine.dispose()  # a second inspect must not need the database at all
        assert await service.inspect(_message(_author())) is False  # type: ignore[arg-type]
        assert apply_ban.await_count == 0
    finally:
        await engine.dispose()


async def test_inspect_skips_bot_and_webhook(tmp_path: Path) -> None:
    bot, engine, service, _claims, _posts, _submits, apply_ban = await _runtime(tmp_path)
    try:
        await _seed_settings(bot.sessions)
        await service.bind(GUILD_ID)
        bot_message = _message(_author(bot=True))
        hook = _message(_author(), webhook_id=9)
        assert await service.inspect(bot_message) is True  # type: ignore[arg-type]
        assert await service.inspect(hook) is True  # type: ignore[arg-type]
        assert apply_ban.await_count == 0
        bot_message.delete.assert_not_called()
        hook.delete.assert_not_called()
    finally:
        await engine.dispose()


async def test_note_join_emits_spike_once_per_thirty_minutes(tmp_path: Path) -> None:
    bot, engine, service, _claims, posts, _submits, _ban = await _runtime(tmp_path)
    try:
        later = NOW + timedelta(minutes=10)
        much_later = NOW + timedelta(minutes=30)
        async with session_scope(bot.sessions) as session:
            for index in range(JOIN_SPIKE_FLOOR):
                session.add(
                    MemberRecord(
                        guild_id=GUILD_ID,
                        user_id=1000 + index,
                        username=f"u{index}",
                        display_name=f"u{index}",
                        first_joined_at=NOW,
                        last_joined_at=NOW,
                    )
                )
        guild = SimpleNamespace(id=GUILD_ID)
        assert await service.note_join(guild, now=NOW) is True
        assert await service.note_join(guild, now=later) is False
        async with session_scope(bot.sessions) as session:
            rows = (await session.scalars(select(MemberRecord).where(MemberRecord.guild_id == GUILD_ID))).all()
            for row in rows:
                row.last_joined_at = much_later
        assert await service.note_join(guild, now=much_later) is True
        assert len(posts) == 2
        assert posts[0]["action"] == ACTION_JOIN_SPIKE
        assert posts[0]["ping_chief"] is True
        assert copy.JOIN_SPIKE_HINT in str(posts[0]["details"])
        few = SimpleNamespace(id=2)
        async with session_scope(bot.sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=2,
                    user_id=1,
                    username="one",
                    display_name="one",
                    first_joined_at=NOW,
                    last_joined_at=NOW,
                )
            )
        assert await service.note_join(few, now=NOW) is False
    finally:
        await engine.dispose()


async def test_funnel_counts_from_existing_tables(tmp_path: Path) -> None:
    bot, engine, service, _claims, _posts, _submits, _ban = await _runtime(tmp_path)
    try:
        inside = NOW - timedelta(days=1)
        old = NOW - timedelta(days=10)
        async with session_scope(bot.sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=GUILD_ID,
                    user_id=1,
                    username="a",
                    display_name="a",
                    first_joined_at=inside,
                    last_joined_at=inside,
                )
            )
            session.add(
                MemberRecord(
                    guild_id=GUILD_ID,
                    user_id=2,
                    username="b",
                    display_name="b",
                    first_joined_at=inside,
                    last_joined_at=inside,
                    left_at=inside + timedelta(hours=3),
                )
            )
            session.add(
                MemberRecord(
                    guild_id=GUILD_ID,
                    user_id=3,
                    username="c",
                    display_name="c",
                    first_joined_at=inside,
                    last_joined_at=inside,
                    left_at=inside + timedelta(hours=48),
                )
            )
            session.add(
                MemberRecord(
                    guild_id=GUILD_ID,
                    user_id=4,
                    username="bot",
                    display_name="bot",
                    is_bot=True,
                    first_joined_at=inside,
                    last_joined_at=inside,
                )
            )
            session.add(
                MemberRecord(
                    guild_id=GUILD_ID,
                    user_id=5,
                    username="old",
                    display_name="old",
                    first_joined_at=old,
                    last_joined_at=old,
                )
            )
            session.add(
                RoleChange(
                    guild_id=GUILD_ID,
                    user_id=1,
                    subsystem=RoleSubsystem.VERIFICATION.value,
                    add_role_ids="[88]",
                    remove_role_ids="[]",
                    reason="Conductor: верификация",
                    ok=True,
                    created_at=inside,
                )
            )
            session.add(
                RoleChange(
                    guild_id=GUILD_ID,
                    user_id=2,
                    subsystem=RoleSubsystem.VERIFICATION.value,
                    add_role_ids="[]",
                    remove_role_ids="[]",
                    reason="noop",
                    ok=True,
                    created_at=inside,
                )
            )
            session.add(
                RoleChange(
                    guild_id=GUILD_ID,
                    user_id=3,
                    subsystem=RoleSubsystem.LOYALTY.value,
                    add_role_ids="[1]",
                    remove_role_ids="[]",
                    reason="old",
                    ok=True,
                    created_at=inside,
                )
            )
            session.add(
                ModCase(
                    guild_id=GUILD_ID,
                    number=1,
                    owner_id=9,
                    created_by=10,
                    actor_rank=4,
                    kind=CaseKind.BAN.value,
                    reason=TRAP_REASON,
                    created_at=inside,
                )
            )
            session.add(
                ModCase(
                    guild_id=GUILD_ID,
                    number=2,
                    owner_id=8,
                    created_by=10,
                    actor_rank=4,
                    kind=CaseKind.BAN.value,
                    reason="другая причина бана здесь",
                    created_at=inside,
                )
            )
        counts = await service.funnel(GUILD_ID, 7, now=NOW)
        assert counts == FunnelCounts(joined=3, verified=1, trapped=1, left_within_24h=1)
    finally:
        await engine.dispose()


async def test_guard_panel_survives_missing_live_row(tmp_path: Path) -> None:
    _bot, engine, service, _claims, _posts, _submits, _ban = await _runtime(tmp_path)
    try:
        resource = await service.guard_panel(GUILD_ID, str(GUILD_ID))
        assert resource is not None
        assert resource.closed is False
        foreign = await service.guard_panel(GUILD_ID, "99")
        assert foreign is not None
        assert foreign.closed is True
    finally:
        await engine.dispose()


async def test_trap_writable_reads_everyone_overwrite(tmp_path: Path) -> None:
    _bot, engine, service, _claims, _posts, _submits, _ban = await _runtime(tmp_path)
    try:
        await _seed_settings(service._bot.sessions)
        await service.bind(GUILD_ID)
        everyone = object()
        channel = SimpleNamespace(
            overwrites_for=lambda _role: SimpleNamespace(send_messages=False),
            permissions_for=lambda _role: SimpleNamespace(send_messages=True),
        )
        guild = SimpleNamespace(id=GUILD_ID, default_role=everyone, get_channel=lambda _cid: channel)
        assert service.trap_writable(guild) is False
    finally:
        await engine.dispose()
