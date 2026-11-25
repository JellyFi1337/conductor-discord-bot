from __future__ import annotations

from types import SimpleNamespace

from discord import app_commands

from conductor.domain.errors import (
    CriticalError,
    ErrorKind,
    error_code_of,
    error_origin,
    held_permission_attrs,
    is_critical_error,
    sanitize_error_text,
    unwrap_error,
)
from conductor.presentation import copy
from conductor.presentation.errors import critical_error_card
from conductor.presentation.layout import TEXT_LIMIT, collected_text, error_code
from conductor.services.errors import CriticalLog


def test_unwrap_and_skip_expected_errors() -> None:
    inner = RuntimeError("disk")

    class _Wrap(Exception):
        def __init__(self, original: Exception) -> None:
            self.original = original

    wrapped = _Wrap(inner)
    assert unwrap_error(wrapped) is inner
    assert is_critical_error(wrapped) is True
    assert is_critical_error(app_commands.CheckFailure("no")) is False
    assert error_code_of(0x4173) == "E-4173"
    assert sanitize_error_text("  boom   boom  ") == "boom boom"


def test_error_origin_prefers_conductor_frame() -> None:
    def boom() -> None:
        raise RuntimeError("x")

    try:
        boom()
    except RuntimeError as exc:
        origin = error_origin(exc)
    assert "boom" in origin
    assert "test_errors.py" in origin


def test_held_permissions_and_card() -> None:
    perms = SimpleNamespace(administrator=True, manage_roles=True, ban_members=False, kick_members=False)
    for attr in (
        "manage_guild",
        "manage_channels",
        "moderate_members",
        "manage_messages",
        "mention_everyone",
        "manage_webhooks",
        "view_audit_log",
        "send_messages",
    ):
        setattr(perms, attr, False)
    assert held_permission_attrs(perms) == ("administrator", "manage_roles")
    report = CriticalError(
        code="E-4173",
        kind=ErrorKind.COMMAND,
        action="/owner seed",
        error_type="RuntimeError",
        error_text="boom",
        origin="ops.py:12 seed",
        guild_id=1,
        guild_name="Hall",
        channel_id=2,
        channel_name="верификация",
        channel_kind="текстовый",
        user_id=7,
        user_name="Ada",
        is_developer=True,
        is_guild_owner=True,
        is_administrator=False,
        rank="chief",
        discord_perms=("manage_roles",),
        missing_bot_perms=("kick_members",),
        extras="with_old=true",
    )
    text = collected_text(critical_error_card(report))
    assert copy.ERROR_TITLE_COMMAND in text
    assert "E-4173" in text
    assert "/owner seed" in text
    assert "with_old=true" in text
    assert "<@7>" in text
    assert copy.ERROR_DEVELOPER in text
    assert copy.ERROR_GUILD_OWNER in text
    assert "верификация" in text
    assert "ops.py:12" in text
    assert "RuntimeError" in text
    assert len(text) < TEXT_LIMIT


async def test_critical_log_posts_to_admin_channel() -> None:
    submitted: list[tuple[int, object]] = []
    bot = SimpleNamespace(
        is_developer=lambda user_id: user_id == 7,
        config_by_guild={9: SimpleNamespace(admin_log_channel_id=55)},
        log_pump=SimpleNamespace(submit=lambda channel_id, view=None, **_k: submitted.append((channel_id, view))),
    )
    service = CriticalLog(bot)
    interaction = SimpleNamespace(
        id=0x4173,
        guild=SimpleNamespace(id=9, name="Hall", owner_id=7, me=SimpleNamespace(guild_permissions=None)),
        channel=SimpleNamespace(id=2, name="верификация", type=SimpleNamespace(name="text")),
        user=SimpleNamespace(
            id=7,
            name="Ada",
            display_name="Ada",
            roles=(),
            guild_permissions=SimpleNamespace(administrator=True, manage_roles=True),
        ),
        command=SimpleNamespace(qualified_name="owner seed"),
        type=SimpleNamespace(name="application_command"),
        namespace=SimpleNamespace(with_old=True),
        data=None,
    )
    report = await service.report_interaction(interaction, RuntimeError("boom"))  # type: ignore[arg-type]
    assert report is not None
    assert report.action == "/owner seed"
    assert report.is_developer is True
    assert "with_old=True" in report.extras
    assert submitted[0][0] == 55
    text = collected_text(submitted[0][1])  # type: ignore[arg-type]
    assert copy.ERROR_TITLE_COMMAND in text
    skipped = await service.report_interaction(interaction, app_commands.CheckFailure("no"))  # type: ignore[arg-type]
    assert skipped is None
    assert len(submitted) == 1


async def test_critical_log_survives_broken_context() -> None:
    bot = SimpleNamespace(
        is_developer=lambda _uid: (_ for _ in ()).throw(RuntimeError("dev")),
        config_by_guild=None,
        log_pump=SimpleNamespace(submit=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("pump"))),
    )
    service = CriticalLog(bot)
    report = await service.report_interaction(SimpleNamespace(), RuntimeError("boom"))  # type: ignore[arg-type]
    assert report is not None
    assert report.code == "E-0000"
    assert report.user_id is None
    guild = SimpleNamespace(id=9, name="Hall", owner_id=1, me=None)
    posted = await service.report_listener(
        "on_guild_channel_create",
        RuntimeError("x"),
        SimpleNamespace(guild=guild),
    )
    assert posted is not None
    assert await service.report_task("seed-ingest-9", RuntimeError("y")) is not None


async def test_critical_log_throttles_repeat_admin_cards() -> None:
    submitted: list[int] = []
    bot = SimpleNamespace(
        is_developer=lambda user_id: user_id == 7,
        config_by_guild={9: SimpleNamespace(admin_log_channel_id=55)},
        log_pump=SimpleNamespace(submit=lambda channel_id, view=None, **_k: submitted.append(channel_id)),
    )
    service = CriticalLog(bot)
    interaction = SimpleNamespace(
        id=0x4173,
        guild=SimpleNamespace(id=9, name="Hall", owner_id=7, me=None),
        channel=SimpleNamespace(id=2, name="log", type=SimpleNamespace(name="text")),
        user=SimpleNamespace(id=7, name="Ada", display_name="Ada", roles=(), guild_permissions=None),
        command=SimpleNamespace(qualified_name="owner seed"),
        type=SimpleNamespace(name="application_command"),
        namespace=None,
        data=None,
    )
    first = await service.report_interaction(interaction, RuntimeError("boom"))  # type: ignore[arg-type]
    second = await service.report_interaction(interaction, RuntimeError("boom"))  # type: ignore[arg-type]
    assert first is not None
    assert first.code == "E-4173"
    assert first.code == error_code(interaction)  # type: ignore[arg-type]
    matched = service.lookup(9, "E-4173")
    assert matched is not None
    assert matched.error_type == "RuntimeError"
    assert "discord.com/channels/9/55" in matched.jump_url
    assert second is not None
    assert submitted == [55]


def test_command_on_cooldown_signature_guard() -> None:
    """discord.py cooldown objects vary; skip by type name even if construct fails."""
    error = app_commands.CheckFailure("x")
    error.__class__ = type("CommandOnCooldown", (app_commands.AppCommandError,), {})
    assert is_critical_error(error) is False
