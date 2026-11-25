from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands, ui

from conductor.bot import (
    _enum_int,
    command_hash_path,
    read_command_hash,
    snapshot_tree,
    tree_fingerprint,
    write_command_hash,
)
from conductor.cogs.diagnostics import (
    CheckStatus,
    DiagnoseSnapshot,
    Diagnostics,
    LogChannelView,
    ManagedRoleView,
    build_diagnose_lines,
    collect_diagnose_snapshot,
    diagnose_card,
    render_diagnose,
)
from conductor.config import Settings
from conductor.domain.observe import TAB_NOW, TAB_READY, TAB_SETUP, TAB_SKELETON
from conductor.domain.permissions import REQUIRED_PERMISSIONS
from conductor.domain.seed import LAYOUT, SKELETON, SkeletonCheck, SkeletonStatus
from conductor.presentation import copy
from conductor.presentation.diagnose import SELECT_PAGE_SIZE, skeleton_page_count
from conductor.presentation.layout import collected_text


def test_enum_int_accepts_python313_enum_proxy() -> None:
    class FakeEnumValue:
        def __init__(self, value: int) -> None:
            self.value = value

        def __int__(self) -> int:
            raise TypeError("not a real int")

    assert _enum_int(3) == 3
    assert _enum_int(FakeEnumValue(7)) == 7
    assert _enum_int(FakeEnumValue(FakeEnumValue(4))) == 4


def _command_row(path: str, description: str = "d") -> dict:
    return {
        "path": path,
        "kind": "command",
        "description": description,
        "nsfw": False,
        "guild_only": True,
        "default_permissions": None,
        "params": [],
    }


def test_tree_fingerprint_is_stable_between_runs() -> None:
    rows = [_command_row("admin panel"), _command_row("diagnose")]
    first = tree_fingerprint(rows, scope=1)
    second = tree_fingerprint(list(rows), scope=1)
    assert first == second
    assert len(first) == 64


def test_tree_fingerprint_changes_when_command_added() -> None:
    base = [_command_row("diagnose")]
    extra = [_command_row("diagnose"), _command_row("admin panel")]
    assert tree_fingerprint(base, scope=1) != tree_fingerprint(extra, scope=1)


def test_tree_fingerprint_changes_when_description_changes() -> None:
    before = [_command_row("diagnose", "a")]
    after = [_command_row("diagnose", "b")]
    assert tree_fingerprint(before, scope=1) != tree_fingerprint(after, scope=1)


def test_tree_fingerprint_includes_sync_scope() -> None:
    rows = [_command_row("diagnose")]
    assert tree_fingerprint(rows, scope=1) != tree_fingerprint(rows, scope=None)


def test_snapshot_tree_hash_changes_when_real_command_added() -> None:
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)

    @tree.command(name="alpha", description="first")
    async def alpha(interaction: discord.Interaction) -> None:
        return None

    before = tree_fingerprint(snapshot_tree(tree), scope=99)

    @tree.command(name="beta", description="second")
    async def beta(interaction: discord.Interaction) -> None:
        return None

    after = tree_fingerprint(snapshot_tree(tree), scope=99)
    assert before != after
    assert tree_fingerprint(snapshot_tree(tree), scope=99) == after


def test_command_hash_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / ".command-hash"
    digest = "ab" * 32
    write_command_hash(path, digest)
    assert read_command_hash(path) == digest
    assert read_command_hash(tmp_path / "missing") is None


def test_command_hash_path_lives_under_data_dir(tmp_path: Path) -> None:
    db = tmp_path / "conductor.db"
    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="t",
        DATA_DIR=str(tmp_path),
        DATABASE_URL=f"sqlite+aiosqlite:///{db.as_posix()}",
    )
    assert command_hash_path(settings) == tmp_path / ".command-hash"


def _perms(*, missing: str | None = None) -> SimpleNamespace:
    flags = {item.attr: True for item in REQUIRED_PERMISSIONS}
    if missing:
        flags[missing] = False
    return SimpleNamespace(**flags)


def _log_ok(name: str = "logs") -> LogChannelView:
    return LogChannelView(configured=True, name=name, can_send=True, can_embed=True)


def _snapshot(**overrides: object) -> DiagnoseSnapshot:
    values: dict[str, object] = {
        "members_intent": True,
        "message_content_intent": True,
        "members_loaded": 10,
        "member_count": 10,
        "permissions": {item.attr: True for item in REQUIRED_PERMISSIONS},
        "bot_top_position": 10,
        "managed_roles": (ManagedRoleView("Ruby Old", 4),),
        "user_log": _log_ok("user-logs"),
        "admin_log": _log_ok("admin-logs"),
        "bot_mode": "live",
        "guild_served": True,
        "latency_ms": 42,
        "log_backlog": 0,
        "log_sent": 3,
        "log_failed": 0,
        "log_dropped": 0,
        "log_pump_alive": True,
        "unread_count": 0,
        "unread_names": (),
    }
    values.update(overrides)
    return DiagnoseSnapshot(**values)  # type: ignore[arg-type]


def test_missing_view_audit_log_is_a_named_fail_line() -> None:
    snap = _snapshot(permissions=_perms(missing="view_audit_log").__dict__)
    lines = build_diagnose_lines(snap)
    fails = [line for line in lines if line.status is CheckStatus.FAIL]
    assert any("View Audit Log" in line.title for line in fails)
    text = render_diagnose(lines)
    assert "View Audit Log" in text
    assert "✕" in text
    assert "Просмотр журнала аудита" in text


def test_missing_moderate_members_is_a_named_fail_line() -> None:
    snap = _snapshot(permissions=_perms(missing="moderate_members").__dict__)
    lines = build_diagnose_lines(snap)
    fails = [line for line in lines if line.status is CheckStatus.FAIL]
    assert any("Moderate Members" in line.title for line in fails)
    text = render_diagnose(lines)
    assert "✕" in text
    assert "Отстранять участников" in text


def test_missing_manage_messages_is_a_named_fail_line() -> None:
    snap = _snapshot(permissions=_perms(missing="manage_messages").__dict__)
    lines = build_diagnose_lines(snap)
    fails = [line for line in lines if line.status is CheckStatus.FAIL]
    assert any("Manage Messages" in line.title for line in fails)
    text = render_diagnose(lines)
    assert "✕" in text
    assert "Управлять сообщениями" in text


def test_missing_ban_members_is_a_named_fail_line() -> None:
    snap = _snapshot(permissions=_perms(missing="ban_members").__dict__)
    lines = build_diagnose_lines(snap)
    fails = [line for line in lines if line.status is CheckStatus.FAIL]
    assert any("Ban Members" in line.title for line in fails)
    text = render_diagnose(lines)
    assert "✕" in text
    assert "Банить участников" in text


def test_verification_channel_not_writable_is_a_warn() -> None:
    lines = build_diagnose_lines(_snapshot(verification_writable=False))
    warns = [line for line in lines if line.status is CheckStatus.WARN]
    assert any(copy.DIAGNOSE_VERIFICATION_WRITE_OFF in line.title for line in warns)
    ok_lines = build_diagnose_lines(_snapshot(verification_writable=True))
    assert any(copy.DIAGNOSE_VERIFICATION_WRITE_OK in line.title for line in ok_lines)


def test_all_permissions_present_are_ok() -> None:
    lines = build_diagnose_lines(_snapshot())
    perm_lines = [line for line in lines if line.title.startswith("Право ")]
    assert len(perm_lines) == len(REQUIRED_PERMISSIONS)
    assert all(line.status is CheckStatus.OK for line in perm_lines)


def test_diagnose_report_is_plain_text_without_nones() -> None:
    text = render_diagnose(build_diagnose_lines(_snapshot()))
    assert "None" not in text
    assert "0x" not in text
    assert "уровень 0 — 0" in text


def test_diagnose_reputation_dry_bans_are_a_warn() -> None:
    lines = build_diagnose_lines(_snapshot(rep_at_threshold=4, rep_would_ban=2))
    found = [line for line in lines if "уровень 0 — 4" in line.title]
    assert found
    assert found[0].status is CheckStatus.WARN
    assert "2" in found[0].title


def test_collect_snapshot_reads_guild_permissions() -> None:
    channel = SimpleNamespace(
        id=11,
        name="user-logs",
        permissions_for=lambda _me: SimpleNamespace(send_messages=True, embed_links=True),
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(guild_permissions=_perms(missing="manage_roles"), top_role=SimpleNamespace(position=8)),
        member_count=2,
        members=[object(), object()],
        text_channels=[SimpleNamespace(id=20, name="general")],
        voice_channels=[],
        stage_channels=[],
        threads=[],
        get_channel=lambda channel_id: channel if channel_id == 11 else None,
        get_role=lambda _role_id: SimpleNamespace(position=3),
    )
    bot = SimpleNamespace(
        intents=SimpleNamespace(members=True, message_content=True),
        latency=0.05,
        history=SimpleNamespace(is_caught_up=lambda *_args: False),
        log_pump=SimpleNamespace(
            backlog=1,
            sent=4,
            failed=0,
            dropped=0,
            _task=SimpleNamespace(done=lambda: False),
        ),
        fence=SimpleNamespace(mode=SimpleNamespace(value="live")),
        serves=lambda _gid: True,
    )
    config = SimpleNamespace(
        user_log_channel_id=11,
        admin_log_channel_id=None,
        ruby_role_id=100,
        emerald_role_id=None,
        sapphire_role_id=None,
        amethyst_role_id=None,
    )
    snap = collect_diagnose_snapshot(bot, guild, config)
    text = render_diagnose(build_diagnose_lines(snap))
    assert "Нет права Manage Roles" in text
    assert f"#{LAYOUT.admin_logs}" in text
    assert "Непрочитанных каналов истории: 1" in text
    assert "#general" in text


def test_unread_log_channels_are_skipped() -> None:
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(guild_permissions=_perms(), top_role=SimpleNamespace(position=8)),
        member_count=1,
        members=[object()],
        text_channels=[SimpleNamespace(id=11, name="user-logs")],
        voice_channels=[],
        stage_channels=[],
        threads=[],
        get_channel=lambda _channel_id: None,
        get_role=lambda _role_id: None,
    )
    bot = SimpleNamespace(
        intents=SimpleNamespace(members=True, message_content=True),
        latency=0.01,
        history=SimpleNamespace(is_caught_up=lambda *_args: False),
        log_pump=SimpleNamespace(backlog=0, sent=0, failed=0, dropped=0, _task=SimpleNamespace(done=lambda: False)),
        fence=SimpleNamespace(mode=SimpleNamespace(value="live")),
        serves=lambda _gid: True,
    )
    config = SimpleNamespace(
        user_log_channel_id=11,
        admin_log_channel_id=None,
        ruby_role_id=None,
        emerald_role_id=None,
        sapphire_role_id=None,
        amethyst_role_id=None,
    )
    snap = collect_diagnose_snapshot(bot, guild, config)
    assert snap.unread_count == 0


def test_role_below_managed_fails() -> None:
    snap = _snapshot(
        bot_top_position=2,
        managed_roles=(ManagedRoleView("Ruby Old", 5), ManagedRoleView("Emerald Old", 1)),
    )
    text = render_diagnose(build_diagnose_lines(snap))
    fail_line = next(line for line in text.splitlines() if "ниже управляемых" in line)
    assert "Ruby Old" in fail_line
    assert "Emerald Old" not in fail_line
    assert "Поднимите роль бота выше" in text


def test_disabled_intents_tell_how_to_fix() -> None:
    snap = _snapshot(members_intent=False, message_content_intent=False)
    text = render_diagnose(build_diagnose_lines(snap))
    assert "Server Members Intent" in text
    assert "Message Content Intent" in text
    assert "Discord Developer Portal" in text


def test_diagnose_reports_which_icon_set_is_active() -> None:
    fallback = render_diagnose(build_diagnose_lines(_snapshot()))
    assert "запасной текстовый набор" in fallback
    loaded = render_diagnose(build_diagnose_lines(_snapshot(emoji_loaded=8, emoji_total=8)))
    assert "Значки движения загружены (8 из 8)" in loaded


def test_diagnose_shows_cache_and_limiter_counters() -> None:
    text = render_diagnose(build_diagnose_lines(_snapshot()))
    assert "Кэш: попадания 0" in text
    assert "Исходящие Discord 0 из 8" in text
    busy = render_diagnose(build_diagnose_lines(_snapshot(limiter_trips=4, outbound_depth=2)))
    assert "лимитер нажатий сработал 4" in busy
    assert "Лимит 50 обновлений в минуту" in busy


def test_setup_tab_is_config_not_runtime() -> None:
    snap = _snapshot(cache_hits=9, queue_jobs=4, db_ok=False)
    titles = [line.title for line in build_diagnose_lines(snap, tab=TAB_SETUP)]
    joined = "\n".join(titles)
    assert "Server Members" in joined
    assert "View Audit Log" in joined
    assert "Кэш:" not in joined
    assert "Очереди:" not in joined
    assert copy.DIAGNOSE_DB_DOWN not in joined


def test_now_tab_is_runtime_not_config() -> None:
    snap = _snapshot(
        cache_hits=9,
        queue_jobs=4,
        queue_later=1,
        bg_tasks=2,
        scheduler_jobs=3,
        emoji_loaded=8,
        emoji_total=8,
        broker_streak=3,
        db_ok=False,
    )
    titles = [line.title for line in build_diagnose_lines(snap, tab=TAB_NOW)]
    joined = "\n".join(titles)
    assert "Кэш: попадания 9" in joined
    assert "Очереди: работы 4" in joined
    assert "фон 2" in joined
    assert copy.DIAGNOSE_CENSUS_NEVER in joined
    assert copy.DIAGNOSE_DB_DOWN in joined
    assert "Значки движения загружены" in joined
    assert "отказов подряд 3" in joined
    assert copy.diagnose_quota_capped(0) in joined
    assert "YouTube:" not in joined
    assert "View Audit Log" not in joined
    assert "Server Members" not in joined
    assert copy.DIAGNOSE_EVIDENCE_OFF not in joined


def test_now_tab_shows_rest_traffic() -> None:
    titles = [line.title for line in build_diagnose_lines(_snapshot(), tab=TAB_NOW)]
    joined = "\n".join(titles)
    assert "запросов в секунду" in joined
    assert "недопустимых запросов за 10 мин" in joined
    assert "глобальных 429" in joined
    assert "топ маршрутов 429" in joined
    assert "топ маршрутов 403" in joined


def test_now_tab_shows_youtube_health() -> None:
    idle = build_diagnose_lines(_snapshot(youtube_feeds=2), tab=TAB_NOW)
    line = next(item for item in idle if item.title.startswith("YouTube:"))
    assert line.status is CheckStatus.WARN
    assert copy.diagnose_youtube_idle(2) in line.title
    checked = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)
    live = build_diagnose_lines(
        _snapshot(youtube_feeds=1, youtube_last_checked_at=checked),
        tab=TAB_NOW,
    )
    ok = next(item for item in live if item.title.startswith("YouTube:"))
    assert ok.status is CheckStatus.OK
    assert "фидов 1" in ok.title
    setup = build_diagnose_lines(_snapshot(youtube_feeds=2), tab=TAB_SETUP)
    assert all("YouTube:" not in item.title for item in setup)


def test_setup_tab_warns_without_automod_file() -> None:
    snap = _snapshot(automod_file=False)
    line = next(item for item in build_diagnose_lines(snap, tab=TAB_SETUP) if "автомодерации" in item.title)
    assert line.status is CheckStatus.WARN
    assert copy.DIAGNOSE_AUTOMOD_FILE_OFF in line.title


def test_setup_tab_warns_mention_everyone_on_non_staff() -> None:
    snap = _snapshot(mention_everyone_roles=("@everyone", "гостевая"))
    line = next(item for item in build_diagnose_lines(snap, tab=TAB_SETUP) if "упоминать @everyone" in item.title)
    assert line.status is CheckStatus.WARN
    assert "гостевая" in line.title
    assert line.action == copy.DIAGNOSE_MENTION_EVERYONE_ACTION


def test_setup_tab_stats_board_states() -> None:
    off_lines = build_diagnose_lines(_snapshot(stats_board="off"), tab=TAB_SETUP)
    off = next(item for item in off_lines if "статистики" in item.title)
    assert off.status is CheckStatus.WARN
    assert copy.DIAGNOSE_STATS_BOARD_OFF in off.title
    assert off.action == copy.DIAGNOSE_STATS_BOARD_ACTION
    gone_lines = build_diagnose_lines(_snapshot(stats_board="stale"), tab=TAB_SETUP)
    gone = next(item for item in gone_lines if "статистики" in item.title)
    assert gone.status is CheckStatus.WARN
    assert copy.DIAGNOSE_STATS_BOARD_GONE in gone.title
    live_lines = build_diagnose_lines(_snapshot(stats_board="live"), tab=TAB_SETUP)
    live = next(item for item in live_lines if "статистики" in item.title)
    assert live.status is CheckStatus.OK
    assert copy.DIAGNOSE_STATS_BOARD_LIVE in live.title


def test_setup_tab_warns_when_automod_card_stale() -> None:
    snap = _snapshot(automod_card=False)
    line = next(item for item in build_diagnose_lines(snap, tab=TAB_SETUP) if "автомода" in item.title)
    assert line.status is CheckStatus.WARN
    assert copy.DIAGNOSE_AUTOMOD_CARD_STALE in line.title
    assert line.action == copy.DIAGNOSE_AUTOMOD_CARD_ACTION
    live = next(
        item for item in build_diagnose_lines(_snapshot(automod_card=True), tab=TAB_SETUP) if "автомода" in item.title
    )
    assert live.status is CheckStatus.OK
    assert copy.DIAGNOSE_AUTOMOD_CARD_OK in live.title


def test_setup_tab_shows_log_and_evidence_lines() -> None:
    snap = _snapshot(
        evidence_category=False,
        staff_feed=LogChannelView(False, None, False, False),
        ticket_log=LogChannelView(False, None, False, False),
    )
    titles = "\n".join(item.title for item in build_diagnose_lines(snap, tab=TAB_SETUP))
    assert copy.log_ok(copy.LOG_USER, "user-logs") in titles
    assert copy.log_ok(copy.LOG_ADMIN, "admin-logs") in titles
    assert copy.log_unconfigured(copy.LOG_STAFF) in titles
    assert copy.log_unconfigured(copy.LOG_TICKETS) in titles
    assert copy.DIAGNOSE_EVIDENCE_OFF in titles


def test_setup_tab_fails_when_user_log_cannot_send() -> None:
    snap = _snapshot(user_log=LogChannelView(True, "лог-участников", False, True))
    line = next(item for item in build_diagnose_lines(snap, tab=TAB_SETUP) if copy.LOG_USER in item.title)
    assert line.status is CheckStatus.FAIL
    assert copy.log_cannot_write(copy.LOG_USER, "лог-участников") == line.title
    assert copy.LOG_PERM_SEND in line.action


def test_now_tab_quota_capped_is_warn() -> None:
    snap = _snapshot(quota_capped=2)
    line = next(item for item in build_diagnose_lines(snap, tab=TAB_NOW) if "квот" in item.title)
    assert line.status is CheckStatus.WARN
    assert copy.diagnose_quota_capped(2) == line.title


def test_scheme_drift_warns_and_apply_button_is_idempotent() -> None:
    async def on_tab(_interaction: discord.Interaction, _tab: str) -> None:
        return None

    async def on_apply(_interaction: discord.Interaction) -> None:
        return None

    drifted = _snapshot(scheme_drift=4)
    warn = next(line for line in build_diagnose_lines(drifted, tab=TAB_SETUP) if "умолчания прав" in line.title)
    assert warn.status is CheckStatus.WARN
    assert "4 ключей" in warn.title
    view = diagnose_card(
        build_diagnose_lines(drifted, tab=TAB_SETUP),
        tab=TAB_SETUP,
        on_tab=on_tab,
        on_apply=on_apply,
    )
    labels = [item.label for item in view.walk_children() if isinstance(item, ui.Button)]
    assert copy.DIAGNOSE_APPLY_DEFAULTS in labels

    aligned = _snapshot(scheme_drift=0)
    titles = [line.title for line in build_diagnose_lines(aligned, tab=TAB_SETUP)]
    assert all("умолчания прав" not in title for title in titles)


def test_ladder_defaults_warn_shows_apply_button() -> None:
    async def on_tab(_interaction: discord.Interaction, _tab: str) -> None:
        return None

    async def on_apply(_interaction: discord.Interaction) -> None:
        return None

    drifted = _snapshot(ladder_defaults=True)
    warn = next(line for line in build_diagnose_lines(drifted, tab=TAB_SETUP) if "умолчания лестницы" in line.title)
    assert warn.status is CheckStatus.WARN
    view = diagnose_card(
        build_diagnose_lines(drifted, tab=TAB_SETUP),
        tab=TAB_SETUP,
        on_tab=on_tab,
        on_apply=on_apply,
    )
    labels = [item.label for item in view.walk_children() if isinstance(item, ui.Button)]
    assert copy.DIAGNOSE_APPLY_DEFAULTS in labels
    aligned = _snapshot(ladder_defaults=False)
    again = diagnose_card(
        build_diagnose_lines(aligned, tab=TAB_SETUP),
        tab=TAB_SETUP,
        on_tab=on_tab,
        on_apply=None,
    )
    again_labels = [item.label for item in again.walk_children() if isinstance(item, ui.Button)]
    assert copy.DIAGNOSE_APPLY_DEFAULTS not in again_labels


def test_rep_defaults_warn_shows_apply_button() -> None:
    async def on_tab(_interaction: discord.Interaction, _tab: str) -> None:
        return None

    async def on_apply(_interaction: discord.Interaction) -> None:
        return None

    drifted = _snapshot(rep_defaults=True)
    warn = next(line for line in build_diagnose_lines(drifted, tab=TAB_SETUP) if "умолчания репутации" in line.title)
    assert warn.status is CheckStatus.WARN
    view = diagnose_card(
        build_diagnose_lines(drifted, tab=TAB_SETUP),
        tab=TAB_SETUP,
        on_tab=on_tab,
        on_apply=on_apply,
    )
    labels = [item.label for item in view.walk_children() if isinstance(item, ui.Button)]
    assert copy.DIAGNOSE_APPLY_DEFAULTS in labels
    aligned = _snapshot(rep_defaults=False)
    again = diagnose_card(
        build_diagnose_lines(aligned, tab=TAB_SETUP),
        tab=TAB_SETUP,
        on_tab=on_tab,
        on_apply=None,
    )
    again_labels = [item.label for item in again.walk_children() if isinstance(item, ui.Button)]
    assert copy.DIAGNOSE_APPLY_DEFAULTS not in again_labels


def test_diagnose_card_tabs_switch_title() -> None:
    async def on_tab(_interaction: discord.Interaction, _tab: str) -> None:
        return None

    lines = build_diagnose_lines(_snapshot(), tab=TAB_SETUP)
    view = diagnose_card(lines, tab=TAB_SETUP, on_tab=on_tab)
    shown = collected_text(view)
    assert copy.DIAGNOSE_TITLE_SETUP in shown
    buttons = [item for item in view.walk_children() if isinstance(item, ui.Button)]
    labels = [button.label for button in buttons]
    assert copy.DIAGNOSE_TAB_SETUP in labels
    assert copy.DIAGNOSE_TAB_SKELETON in labels
    assert copy.DIAGNOSE_TAB_READY in labels
    assert copy.DIAGNOSE_TAB_NOW in labels
    setup = next(button for button in buttons if button.label == copy.DIAGNOSE_TAB_SETUP)
    assert setup.disabled is True


def test_collect_snapshot_reads_queues_and_census() -> None:
    when = datetime(2026, 9, 1, tzinfo=UTC)
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(guild_permissions=_perms(), top_role=SimpleNamespace(position=8)),
        member_count=1,
        members=[object()],
        text_channels=[],
        voice_channels=[],
        stage_channels=[],
        threads=[],
        get_channel=lambda _channel_id: None,
        get_role=lambda _role_id: None,
    )
    bot = SimpleNamespace(
        intents=SimpleNamespace(members=True, message_content=True),
        latency=0.01,
        history=SimpleNamespace(is_caught_up=lambda *_args: True),
        log_pump=SimpleNamespace(backlog=0, sent=0, failed=0, dropped=0, _task=SimpleNamespace(done=lambda: False)),
        fence=SimpleNamespace(mode=SimpleNamespace(value="live")),
        serves=lambda _gid: True,
        queue=SimpleNamespace(job_count=7, later_count=2),
        scheduler=SimpleNamespace(job_count=5),
        _bg_tasks={object(), object()},
        motion=SimpleNamespace(loaded_count=3, total_count=8),
    )
    config = SimpleNamespace(
        user_log_channel_id=None,
        admin_log_channel_id=None,
        ruby_role_id=None,
        emerald_role_id=None,
        sapphire_role_id=None,
        amethyst_role_id=None,
        census_at=when,
    )
    snap = collect_diagnose_snapshot(bot, guild, config)
    assert snap.queue_jobs == 7
    assert snap.queue_later == 2
    assert snap.bg_tasks == 2
    assert snap.scheduler_jobs == 5
    assert snap.census_at == when
    assert snap.emoji_loaded == 3
    text = "\n".join(line.title for line in build_diagnose_lines(snap, tab=TAB_NOW))
    assert "работы 7" in text
    assert "Последняя сверка состава:" in text
    assert copy.DIAGNOSE_CENSUS_NEVER not in text


class _AckTrace:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.response = _AckResponse(self.order)
        self.guild = SimpleNamespace(id=1)

    async def edit_original_response(self, **_kwargs: object) -> None:
        self.order.append("edit_original_response")


class _AckResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def is_done(self) -> bool:
        return "defer" in self._order

    async def defer(self, **_kwargs: object) -> None:
        self._order.append("defer")


async def test_diagnose_switch_defers_before_snapshot() -> None:
    cog = Diagnostics(SimpleNamespace())
    interaction = _AckTrace()

    async def fake_card(_guild: object, _tab: str, **_kwargs: object) -> object:
        interaction.order.append("work")
        return diagnose_card((), tab=TAB_NOW)

    cog._card = fake_card  # type: ignore[method-assign]
    await cog._switch(interaction, TAB_NOW)  # type: ignore[arg-type]
    assert interaction.order == ["defer", "work", "edit_original_response"]


def test_external_timeout_ignores_audit_diff_hasattr() -> None:
    from conductor.cogs.admin_logs import _is_external_sanction

    class _AlwaysAttr:
        def __getattr__(self, name: str) -> object:
            return None

    bot = SimpleNamespace(user=SimpleNamespace(id=1))
    entry = SimpleNamespace(
        action=SimpleNamespace(name="member_update"),
        user=SimpleNamespace(id=2),
        after=_AlwaysAttr(),
    )
    assert _is_external_sanction(entry, bot) is False
    entry.after = SimpleNamespace(timed_out_until=object(), communication_disabled_until=None)
    assert _is_external_sanction(entry, bot) is True


def _action_rows(view: ui.LayoutView) -> list[ui.ActionRow]:
    found: list[ui.ActionRow] = []

    def walk(node: object) -> None:
        if isinstance(node, ui.ActionRow):
            found.append(node)
            return
        for child in getattr(node, "children", None) or ():
            walk(child)

    walk(view)
    return found


def test_diagnose_card_layout_limits_on_all_tabs() -> None:
    async def noop(_interaction: discord.Interaction, *_args: object) -> None:
        return None

    snap = _snapshot()
    broken = tuple(
        SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.FAIL, "missing") for entry in SKELETON
    )
    for tab in (TAB_SETUP, TAB_SKELETON, TAB_READY, TAB_NOW):
        pages = skeleton_page_count(snap) if tab == TAB_SKELETON else 1
        for page in range(1, pages + 1):
            view = diagnose_card(
                build_diagnose_lines(snap, tab=tab, page=page),
                tab=tab,
                on_tab=noop,
                on_repair=noop if tab == TAB_SKELETON else None,
                on_repair_all=noop if tab in {TAB_SKELETON, TAB_READY} else None,
                on_reapply=noop if tab in {TAB_SKELETON, TAB_READY} else None,
                on_map=noop if tab in {TAB_SKELETON, TAB_READY} else None,
                on_page=noop if tab == TAB_SKELETON and pages > 1 else None,
                broken=broken,
                page=page,
                pages=pages,
            )
            rows = _action_rows(view)
            assert 1 <= len(rows) <= 5
            for row in rows:
                for item in getattr(row, "children", ()) or ():
                    options = getattr(item, "options", None)
                    if options is not None:
                        assert len(options) <= SELECT_PAGE_SIZE
