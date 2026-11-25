from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from discord import app_commands

from conductor.bot import COGS, Conductor
from conductor.domain.access import (
    ACTIVITY_CONFIG,
    CONFIG_MANAGE,
    DATA_FORGET,
    DATA_MANAGE,
    DIAGNOSE_RUN,
    EVENT_CREATE,
    EVENT_MANAGE,
    GIVEAWAY_CREATE,
    GIVEAWAY_MANAGE,
    GROUP_CREATE,
    HELP_READ,
    LOYALTY_MANAGE,
    MOD_BAN,
    MOD_CHANNEL_LOCK,
    MOD_HISTORY,
    MOD_KICK,
    MOD_MUTE,
    MOD_NOTE,
    MOD_PURGE,
    MOD_TIMEOUT,
    MOD_UNBAN,
    MOD_UNMUTE,
    MOD_WARN,
    MOD_WATCH,
    POLL_CREATE,
    POLL_DEANONYMIZE,
    POLL_MANAGE,
    RECRUIT_MANAGE,
    RECRUIT_READ,
    REMIND_USE,
    REP_CONFIG,
    REP_PARDON,
    REP_SHOW_OTHER,
    STATS_INGEST,
    STATS_READ,
    TICKETS_CLOSE_ANY,
    TICKETS_FORM_MANAGE,
    TICKETS_PANEL_POST,
    TICKETS_REOPEN,
    VOICE_HUB_MANAGE,
    VOICE_PRESET,
    VOICE_STAFF_OVERRIDE,
    WATCHERS_DISMISS,
    WATCHERS_NOTE,
    WATCHERS_READ,
)
from conductor.presentation.access import allow_key, is_guild_admin
from conductor.presentation.copy import FAILURE_TITLE, STALE_CARD, denied_actors
from conductor.presentation.layout import collected_text

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"


def test_administrator_flag_is_not_manage_guild() -> None:
    member = MagicMock(spec=discord.Member)
    member.guild_permissions.administrator = True
    member.guild_permissions.manage_guild = False
    assert is_guild_admin(member) is True
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = True
    assert is_guild_admin(member) is False
    assert is_guild_admin(MagicMock(spec=discord.User)) is False


def test_admin_only_is_gone() -> None:
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "def admin_only" not in text, path
        assert "@admin_only" not in text, path


def _keyed_names(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Name) or dec.func.id != "keyed":
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Name):
                continue
            found[node.name] = dec.args[0].id
    return found


def test_translated_commands_check_their_keys() -> None:
    admin = _keyed_names(SRC / "cogs" / "admin.py")
    assert admin == {"import_": "CONFIG_MANAGE", "panel_cmd": "ADMIN_PANEL_OPEN"}
    stats = _keyed_names(SRC / "cogs" / "stats.py")
    assert stats == {"stats": "STATS_READ"}
    loyalty = _keyed_names(SRC / "cogs" / "loyalty.py")
    assert loyalty == {"loyalty": "LOYALTY_MANAGE"}
    assert _keyed_names(SRC / "cogs" / "diagnostics.py")["diagnose"] == "DIAGNOSE_RUN"
    assert _keyed_names(SRC / "cogs" / "guide.py")["help"] == "HELP_READ"
    assert HELP_READ == "help.read"
    watchers = _keyed_names(SRC / "cogs" / "watchers.py")
    assert watchers["list_"] == "WATCHERS_READ"
    assert "info" not in watchers
    assert watchers["dismiss"] == "WATCHERS_DISMISS"
    assert watchers["note"] == "WATCHERS_NOTE"
    assert "policy" not in watchers
    assert "appoint" not in watchers
    assert "duty" not in watchers
    voice = _keyed_names(SRC / "cogs" / "voice.py")
    assert voice == {}
    assert "hub_set" not in voice
    assert "preset_sequential" not in voice
    assert "preset_clone" not in voice
    tickets = _keyed_names(SRC / "cogs" / "tickets.py")
    assert tickets == {}
    assert "panel_post" not in tickets
    assert "form_questions" not in tickets
    assert _keyed_names(SRC / "cogs" / "dossier.py")["open_member"] == "MOD_HISTORY"
    moderation = _keyed_names(SRC / "cogs" / "moderation.py")
    assert moderation["warn"] == "MOD_WARN"
    assert moderation["timeout"] == "MOD_TIMEOUT"
    assert moderation["mute"] == "MOD_MUTE"
    assert moderation["unmute"] == "MOD_UNMUTE"
    assert "note" not in moderation
    assert moderation["kick"] == "MOD_KICK"
    assert moderation["ban"] == "MOD_BAN"
    assert moderation["unban"] == "MOD_UNBAN"
    assert "softban" not in moderation
    assert moderation["purge"] == "MOD_PURGE"
    assert moderation["lock"] == "MOD_CHANNEL_LOCK"
    assert "unlock" not in moderation
    assert moderation["history"] == "MOD_HISTORY"
    assert moderation["pardon"] == "REP_PARDON"
    assert "muterole" not in moderation
    profile_src = (SRC / "cogs" / "profile.py").read_text(encoding="utf-8")
    assert "REP_SHOW_OTHER" in profile_src
    assert "allow_key" in profile_src
    giveaway = _keyed_names(SRC / "cogs" / "giveaway.py")
    assert giveaway == {"giveaway": "GIVEAWAY_CREATE"}
    assert "allow" not in giveaway
    assert "deny" not in giveaway
    assert "config" not in giveaway
    assert "cancel" not in giveaway
    assert "reroll" not in giveaway
    poll = _keyed_names(SRC / "cogs" / "poll.py")
    assert poll["start"] == "POLL_CREATE"
    assert poll["deanonymize"] == "POLL_DEANONYMIZE"
    assert "close" not in poll
    assert "delete" not in poll
    events = _keyed_names(SRC / "cogs" / "events.py")
    assert events["event"] == "EVENT_CREATE"
    assert "cancel" not in events
    groups = _keyed_names(SRC / "cogs" / "groups.py")
    assert groups == {}
    groups_src = (SRC / "cogs" / "groups.py").read_text(encoding="utf-8")
    assert "default_permissions" not in groups_src
    assert "@keyed" not in groups_src
    remind = _keyed_names(SRC / "cogs" / "reminders.py")
    assert remind == {"remind": "REMIND_USE"}
    recruit = _keyed_names(SRC / "cogs" / "recruit.py")
    assert recruit == {"link": "RECRUIT_MANAGE", "report": "RECRUIT_READ"}
    assert RECRUIT_MANAGE == "recruit.manage"
    assert RECRUIT_READ == "recruit.read"
    assert _keyed_names(SRC / "cogs" / "ops.py") == {}
    assert STATS_READ == "stats.read"
    assert STATS_INGEST == "stats.ingest"
    assert LOYALTY_MANAGE == "loyalty.manage"
    assert DIAGNOSE_RUN == "diagnose.run"
    assert WATCHERS_READ == "watchers.read"
    assert WATCHERS_DISMISS == "watchers.dismiss"
    assert WATCHERS_NOTE == "watchers.note"
    assert CONFIG_MANAGE == "config.manage"
    assert VOICE_HUB_MANAGE == "voice.hub_manage"
    assert VOICE_STAFF_OVERRIDE == "voice.staff_override"
    assert VOICE_PRESET == "voice.preset"
    assert TICKETS_PANEL_POST == "tickets.panel_post"
    assert TICKETS_FORM_MANAGE == "tickets.form_manage"
    assert TICKETS_REOPEN == "tickets.reopen"
    assert MOD_WARN == "mod.warn"
    assert MOD_TIMEOUT == "mod.timeout"
    assert MOD_MUTE == "mod.mute"
    assert MOD_UNMUTE == "mod.unmute"
    assert MOD_NOTE == "mod.note"
    assert MOD_HISTORY == "mod.history"
    assert MOD_WATCH == "mod.watch"
    assert MOD_KICK == "mod.kick"
    assert MOD_BAN == "mod.ban"
    assert MOD_UNBAN == "mod.unban"
    assert MOD_PURGE == "mod.purge"
    assert MOD_CHANNEL_LOCK == "mod.channel_lock"
    assert REP_SHOW_OTHER == "rep.show_other"
    assert REP_CONFIG == "rep.config"
    assert REP_PARDON == "rep.pardon"
    assert ACTIVITY_CONFIG == "activity.config"
    assert GIVEAWAY_CREATE == "giveaway.create"
    assert GIVEAWAY_MANAGE == "giveaway.manage"
    assert POLL_CREATE == "poll.create"
    assert POLL_MANAGE == "poll.manage"
    assert POLL_DEANONYMIZE == "poll.deanonymize"
    assert EVENT_CREATE == "event.create"
    assert EVENT_MANAGE == "event.manage"
    assert GROUP_CREATE == "group.create"
    assert REMIND_USE == "remind.use"
    assert DATA_MANAGE == "data.manage"
    assert DATA_FORGET == "data.forget"
    assert "panel_cmd" not in voice
    assert "claim_cmd" not in voice
    assert "join_cmd" not in voice
    assert "voice" not in voice


class _Response:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_kwargs: object) -> None:
        self._done = True

    async def send_message(self, **kwargs: object) -> None:
        self.payloads.append(kwargs)
        self._done = True


async def test_allow_key_denies_without_rank() -> None:
    from conductor.domain.access import AccessDecision, Rank

    async def decide_access(**_kwargs: object) -> AccessDecision:
        return AccessDecision(False, Rank.NONE, need=Rank.SPECIAL)

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2, roles=(), guild_permissions=SimpleNamespace(administrator=False)),
        client=SimpleNamespace(decide_access=decide_access),
        response=_Response(),
    )
    allowed = await allow_key(interaction, CONFIG_MANAGE)  # type: ignore[arg-type]
    assert allowed is False
    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert denied_actors("special") in text
    assert STALE_CARD not in text


async def test_app_command_error_sends_failure_card_with_e_code() -> None:
    response = _Response()
    interaction = SimpleNamespace(
        id=0x4173,
        response=response,
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=7),
        command=SimpleNamespace(qualified_name="setup.user-logs"),
    )
    await Conductor._on_app_command_error(
        SimpleNamespace(),  # type: ignore[misc]
        interaction,  # type: ignore[arg-type]
        app_commands.AppCommandError("boom"),
    )
    payload = response.payloads[0]
    assert "embed" not in payload
    text = collected_text(payload["view"])  # type: ignore[arg-type]
    assert "E-4173" in text
    assert FAILURE_TITLE in text


async def test_check_failure_after_keyed_card_is_silent() -> None:
    response = _Response()
    response._done = True
    interaction = SimpleNamespace(
        id=0x4173,
        response=response,
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=7),
        command=SimpleNamespace(qualified_name="setup.user-logs"),
    )
    await Conductor._on_app_command_error(
        SimpleNamespace(),  # type: ignore[misc]
        interaction,  # type: ignore[arg-type]
        app_commands.CheckFailure("denied"),
    )
    assert response.payloads == []


EXPECTED_LEAVES = (
    "/help",
    "/diagnose",
    "/profile",
    "/stats",
    "/remind",
    "/voice",
    "/loyalty",
    "/giveaway",
    "/event",
    "/group",
    "/groups",
    "/bug",
    "/recruit link",
    "/recruit report",
    "/admin panel",
    "/admin import",
    "/mod warn",
    "/mod timeout",
    "/mod mute",
    "/mod unmute",
    "/mod history",
    "/mod kick",
    "/mod ban",
    "/mod unban",
    "/mod purge",
    "/mod lock",
    "/mod pardon",
    "/watchers list",
    "/watchers appoint",
    "/watchers dismiss",
    "/watchers duty",
    "/watchers note",
    "/poll start",
    "/poll deanonymize",
    "/owner wipe",
    "/owner strip",
    "/owner grant",
    "/owner old-import",
    "/owner seed",
    "/owner lift",
    "/owner rep",
    "/owner emojis",
    "/owner limits",
    "/owner export",
    "/owner apply",
)
PARAM_LIMIT = 4
DESCRIPTION_LIMIT = 60
LEAF_LIMIT = 45
CONTEXT_MENU_LIMIT = 1
PARAM_EXCEPTIONS = frozenset({"/owner strip", "/owner grant"})


def _is_app_command(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "command":
        value = func.value
        return isinstance(value, ast.Name) and value.id == "app_commands"
    return False


def _command_meta(node: ast.AsyncFunctionDef) -> tuple[str, str] | None:
    from conductor.presentation import copy

    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call) or not _is_app_command(dec):
            continue
        name = node.name.rstrip("_")
        description = ""
        for kw in dec.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name = str(kw.value.value)
            if kw.arg == "description" and isinstance(kw.value, ast.Attribute):
                owner = kw.value.value
                if isinstance(owner, ast.Name) and owner.id == "copy":
                    description = str(getattr(copy, kw.value.attr))
        return name, description
    return None


def _param_count(node: ast.AsyncFunctionDef) -> int:
    skip = {"self", "interaction"}
    args = [item.arg for item in node.args.args if item.arg not in skip]
    kwonly = [item.arg for item in node.args.kwonlyargs if item.arg not in skip]
    return len(args) + len(kwonly)


def _group_name(cls: ast.ClassDef) -> str | None:
    for kw in cls.keywords:
        if kw.arg == "group_name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def _loaded_cog_paths() -> list[Path]:
    paths: list[Path] = []
    for extension in COGS:
        name = extension.rsplit(".", 1)[-1]
        paths.append(SRC / "cogs" / f"{name}.py")
    return paths


def _leaf_commands() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    groups: list[tuple[str, int]] = []
    for path in _loaded_cog_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            group = _group_name(node)
            leaves: list[tuple[str, int, str]] = []
            for item in node.body:
                if not isinstance(item, ast.AsyncFunctionDef):
                    continue
                meta = _command_meta(item)
                if meta is None:
                    continue
                name, description = meta
                path_name = f"/{group} {name}" if group else f"/{name}"
                leaves.append((path_name, _param_count(item), description))
            if group is not None:
                groups.append((group, len(leaves)))
            found.extend(leaves)
    for group, count in groups:
        assert count != 1, group
        assert count == 0 or count >= 2, group
    return found


def _context_menu_names() -> list[str]:
    from conductor.presentation import copy

    found: list[str] = []
    for path in _loaded_cog_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_menu = (isinstance(func, ast.Attribute) and func.attr == "ContextMenu") or (
                isinstance(func, ast.Name) and func.id == "ContextMenu"
            )
            if not is_menu:
                continue
            name = ""
            for kw in node.keywords:
                if kw.arg != "name":
                    continue
                if isinstance(kw.value, ast.Constant):
                    name = str(kw.value.value)
                elif (
                    isinstance(kw.value, ast.Attribute)
                    and isinstance(kw.value.value, ast.Name)
                    and kw.value.value.id == "copy"
                ):
                    name = str(getattr(copy, kw.value.attr))
            found.append(name)
    return found


def test_leaf_command_tree_matches_target() -> None:
    from conductor.domain.access import ACTION_KEYS
    from conductor.domain.guide import KIND_COMMAND
    from conductor.presentation import copy
    from conductor.presentation.copy.guide_catalog import catalog_command_paths, catalog_entries

    leaves = _leaf_commands()
    paths = tuple(sorted(path for path, _count, _desc in leaves))
    assert paths == tuple(sorted(EXPECTED_LEAVES))
    assert len(paths) <= LEAF_LIMIT
    assert len(paths) == LEAF_LIMIT
    for path, count, description in leaves:
        if path not in PARAM_EXCEPTIONS:
            assert count <= PARAM_LIMIT, path
        assert description
        assert len(description) <= DESCRIPTION_LIMIT, path
    menus = _context_menu_names()
    assert menus == [copy.CMD_DOSSIER]
    assert copy.CMD_DOSSIER == "Досье"
    assert len(menus) == CONTEXT_MENU_LIMIT
    catalog = set(catalog_command_paths())
    assert set(EXPECTED_LEAVES) <= catalog
    for item in catalog_entries():
        assert item.summary.strip(), item.path
        assert item.details, item.path
        assert 2 <= len(item.details) <= 4, item.path
        if item.key is not None:
            assert item.key in ACTION_KEYS, item.key
        if item.kind == KIND_COMMAND:
            assert item.path.startswith("/"), item.path
    ops = (SRC / "cogs" / "ops.py").read_text(encoding="utf-8")
    assert "CMD_OWNER_SEED_MODE" in ops
    assert "mode=SEED_MODE_CHOICES" in ops
    seed_params = next(count for path, count, _desc in leaves if path == "/owner seed")
    assert seed_params == 2
    assert copy.CMD_OWNER_SEED_MODE
    assert copy.CMD_OWNER_SEED_MODE_BUILD
    assert copy.CMD_OWNER_SEED_MODE_ADOPT


async def test_panel_buttons_guard_owner_key_stranger() -> None:
    from conductor.domain.access import AccessDecision, Rank
    from conductor.presentation.giveaway import GiveawayCancelButton
    from conductor.presentation.guard import GuardResource, run_guard
    from conductor.presentation.poll import PollCloseButton, PollDeleteButton
    from conductor.presentation.tickets import TicketRenameButton

    assert PollCloseButton.access_key == POLL_MANAGE
    assert PollDeleteButton.access_key == POLL_MANAGE
    assert GiveawayCancelButton.access_key == GIVEAWAY_MANAGE
    assert TicketRenameButton.access_key == TICKETS_CLOSE_ANY

    async def load_owner(*, guild_id: int, ref: str) -> GuardResource:
        return GuardResource(ref=ref, guild_id=guild_id, owner_id=10)

    cases = (POLL_MANAGE, GIVEAWAY_MANAGE, TICKETS_CLOSE_ANY)
    for key in cases:
        owner = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=10, roles=(), guild_permissions=SimpleNamespace(administrator=False)),
            client=SimpleNamespace(
                interactions=None,
                decide_access=lambda **_k: AccessDecision(False, Rank.NONE, need=Rank.SENIOR),
                log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
                config_by_guild=SimpleNamespace(get=lambda *_a: None),
            ),
            response=_Response(),
        )

        async def decide_owner(**_kwargs: object) -> AccessDecision:
            return AccessDecision(False, Rank.NONE, need=Rank.SENIOR)

        owner.client.decide_access = decide_owner
        owned = await run_guard(owner, ref="8", load=load_owner, key=key)  # type: ignore[arg-type]
        assert owned.ok is True

        async def decide_key(**_kwargs: object) -> AccessDecision:
            return AccessDecision(True, Rank.SENIOR)

        keyed_user = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99, roles=(), guild_permissions=SimpleNamespace(administrator=False)),
            client=SimpleNamespace(
                interactions=None,
                decide_access=decide_key,
                log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
                config_by_guild=SimpleNamespace(get=lambda *_a: None),
            ),
            response=_Response(),
        )
        allowed = await run_guard(keyed_user, ref="8", load=load_owner, key=key)  # type: ignore[arg-type]
        assert allowed.ok is True

        async def decide_stranger(**_kwargs: object) -> AccessDecision:
            return AccessDecision(False, Rank.NONE, need=Rank.SENIOR)

        stranger = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99, roles=(), guild_permissions=SimpleNamespace(administrator=False)),
            client=SimpleNamespace(
                interactions=None,
                decide_access=decide_stranger,
                log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
                config_by_guild=SimpleNamespace(get=lambda *_a: None),
            ),
            response=_Response(),
        )
        denied = await run_guard(stranger, ref="8", load=load_owner, key=key)  # type: ignore[arg-type]
        assert denied.ok is False
        assert denied.reason == "denied"


async def test_lock_duration_zero_unlocks_and_cancels_job() -> None:
    from conductor.cogs.moderation import Moderation
    from conductor.domain.moderation import ModOutcome, ModVerdict, is_unlock_duration

    assert is_unlock_duration("0")
    assert is_unlock_duration("снять")
    assert is_unlock_duration(" Снять ")
    assert not is_unlock_duration("10m")

    unlocked: list[int] = []
    cancelled: list[object] = []

    async def unlock_channel(_guild: object, channel: object, **_kwargs: object) -> ModOutcome:
        unlocked.append(int(channel.id))
        cancelled.append(("mod-unlock", 1, int(channel.id)))
        return ModOutcome(ModVerdict.OK)

    bot = SimpleNamespace(moderation=SimpleNamespace(unlock_channel=unlock_channel))
    cog = Moderation(bot)  # type: ignore[arg-type]
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 40
    channel.mention = "#общий"
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    response = _Response()

    async def followup_send(**kwargs: object) -> None:
        response.payloads.append(kwargs)

    interaction = SimpleNamespace(
        guild=guild,
        channel=channel,
        user=SimpleNamespace(id=2),
        response=response,
        followup=SimpleNamespace(send=followup_send),
    )
    await cog.lock.callback(cog, interaction, duration="0")  # type: ignore[arg-type]
    assert unlocked == [40]
    assert cancelled == [("mod-unlock", 1, 40)]
    from conductor.presentation import copy

    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert copy.MOD_LOCK_CLEARED in text


async def test_remind_without_params_lists_cancel_buttons() -> None:
    from datetime import UTC, datetime

    from conductor.domain.reminders import ReminderCard
    from conductor.presentation.reminders import list_view

    card = ReminderCard(
        reminder_id=4,
        guild_id=1,
        channel_id=2,
        user_id=7,
        text="чай",
        due_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
    )

    async def on_cancel(_interaction: object, _reminder_id: int) -> None:
        return None

    view = list_view((card,), on_cancel=on_cancel)
    text = collected_text(view)
    assert "чай" in text
    assert "Отменить #4" in text
