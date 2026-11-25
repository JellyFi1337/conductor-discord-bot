"""Короткий прогон на живом Discord: забор, /diagnose, команды. Токен не печатает."""

from __future__ import annotations

import asyncio
import json
import sys

from conductor.bot import Conductor
from conductor.config import Settings
from conductor.db.engine import current_revision
from conductor.domain.permissions import REQUIRED_PERMISSIONS
from conductor.logging import configure_logging
from conductor.presentation.copy import CMD_DOSSIER
from conductor.presentation.diagnose import CheckStatus, build_diagnose_lines
from conductor.services.observe import collect_diagnose_snapshot

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
    "/owner emojis",
)
PROBE_PERMISSIONS = (
    "moderate_members",
    "ban_members",
    "view_audit_log",
    "manage_channels",
    "move_members",
)


def _perm_gaps(permissions: dict[str, bool]) -> list[str]:
    return [item.attr for item in REQUIRED_PERMISSIONS if not permissions.get(item.attr, False)]


def _option_kind(option: object) -> int:
    kind = getattr(option, "type", None)
    return int(getattr(kind, "value", 0) or 0)


def _command_kind(command: object) -> int:
    kind = getattr(command, "type", None)
    return int(getattr(kind, "value", 1) or 1)


def _leaf_paths(commands: list[object]) -> list[str]:
    leaves: list[str] = []
    for command in commands:
        if _command_kind(command) != 1:
            continue
        options = list(getattr(command, "options", None) or [])
        groups = [item for item in options if _option_kind(item) == 2]
        subs = [item for item in options if _option_kind(item) == 1]
        if groups:
            for group in groups:
                inner = [item for item in (getattr(group, "options", None) or []) if _option_kind(item) == 1]
                for item in inner:
                    leaves.append(f"/{command.name} {group.name} {item.name}")
            continue
        if subs:
            for item in subs:
                leaves.append(f"/{command.name} {item.name}")
            continue
        leaves.append(f"/{command.name}")
    return sorted(leaves)


def _menu_names(commands: list[object]) -> list[str]:
    return sorted(str(command.name) for command in commands if _command_kind(command) in {2, 3})


async def _revision(bot: Conductor) -> str:
    async with bot.engine.connect() as conn:
        value = await conn.run_sync(current_revision)
    return value or "none"


async def _probe(bot: Conductor, started: asyncio.Task[None]) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + 180
    last = 0.0
    while not bot._booted:
        if started.done():
            error = started.exception()
            raise RuntimeError(f"bot.start завершился до ready: {error!r}")
        now = asyncio.get_running_loop().time()
        if now > deadline:
            raise TimeoutError(
                "бот не успел пройти on_ready за 180 с "
                f"(ready={bot.is_ready()} user={bot.user} guilds={len(bot.guilds)})"
            )
        if now - last >= 5:
            sys.stderr.write(
                f"live_probe: ждём ready={bot.is_ready()} booted={bot._booted} "
                f"user={bot.user} guilds={len(bot.guilds)}\n"
            )
            last = now
        await asyncio.sleep(0.2)

    settings = bot.settings
    fence = bot.fence
    db_ok = await bot.observe.ping_db()
    revision = await _revision(bot)
    present = [{"id": guild.id, "name": guild.name, "served": bot.serves(guild.id)} for guild in bot.guilds]
    foreign = [item for item in present if fence is not None and not fence.may_reside(int(item["id"]))]
    intents = getattr(bot, "intents", None)
    dm_messages = bool(getattr(intents, "dm_messages", False))

    test_id = settings.test_guild_id
    if test_id is None and fence is not None and fence.active_ids:
        test_id = min(fence.active_ids)
    guild = bot.get_guild(test_id) if test_id else None
    test: dict[str, object] = {"present": guild is not None}
    command_ok = False
    menu_ok = False
    diagnose_fail = []
    if guild is not None:
        config = bot.config_by_guild.get(guild.id)
        snap = collect_diagnose_snapshot(bot, guild, config, db_ok=db_ok)
        lines = build_diagnose_lines(snap)
        diagnose_fail = [line.title for line in lines if line.status is CheckStatus.FAIL]
        try:
            synced = list(await bot.tree.fetch_commands(guild=guild))
            command_names = _leaf_paths(synced)
            menu_names = _menu_names(synced)
        except Exception as exc:
            command_names = [f"fetch_failed:{type(exc).__name__}"]
            menu_names = []
        command_ok = command_names == sorted(EXPECTED_LEAVES)
        menu_ok = CMD_DOSSIER in menu_names
        test = {
            "present": True,
            "name": guild.name,
            "served": bot.serves(guild.id),
            "members_loaded": snap.members_loaded,
            "member_count": snap.member_count,
            "latency_ms": snap.latency_ms,
            "permissions_missing": _perm_gaps(dict(snap.permissions)),
            "probe_permissions_missing": [
                attr for attr in PROBE_PERMISSIONS if attr in _perm_gaps(dict(snap.permissions))
            ],
            "bot_top_position": snap.bot_top_position,
            "managed_roles": [{"label": role.label, "position": role.position} for role in snap.managed_roles],
            "user_log": {
                "configured": snap.user_log.configured,
                "can_send": snap.user_log.can_send,
                "can_embed": snap.user_log.can_embed,
            },
            "admin_log": {
                "configured": snap.admin_log.configured,
                "can_send": snap.admin_log.can_send,
                "can_embed": snap.admin_log.can_embed,
            },
            "census_at": None if snap.census_at is None else snap.census_at.isoformat(),
            "commands": command_names,
            "command_count": len(command_names),
            "commands_match_38": command_ok,
            "menus": menu_names,
            "has_dossier_menu": menu_ok,
            "has_help": "/help" in command_names,
            "dm_messages": snap.dm_messages_intent,
            "diagnose_fail": diagnose_fail,
            "diagnose": [{"status": line.status.value, "title": line.title} for line in lines],
        }

    public_id = settings.public_guild_id
    public = bot.get_guild(public_id) if public_id else None
    gaps = list(test.get("permissions_missing") or []) if guild is not None else ["guild_missing"]
    probe_gaps = list(test.get("probe_permissions_missing") or []) if guild is not None else ["guild_missing"]
    return {
        "ok": (
            not foreign
            and guild is not None
            and bot.serves(guild.id)
            and db_ok
            and command_ok
            and menu_ok
            and dm_messages
            and not probe_gaps
        ),
        "mode": settings.mode_label,
        "user": str(bot.user),
        "database": "postgres" if "postgres" in settings.database_url else "sqlite",
        "auto_migrate": settings.auto_migrate_enabled,
        "alembic": revision,
        "db_ok": db_ok,
        "dm_messages": dm_messages,
        "guilds_present": present,
        "foreign_still_here": foreign,
        "public_present": public is not None,
        "public_served": bool(public_id and bot.serves(public_id)),
        "permissions_missing": gaps,
        "test": test,
    }


async def _run() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    if not settings.token_looks_real or settings.fence is None:
        sys.stderr.write("live_probe: нужны DISCORD_TOKEN и GUILD_IDS (или TEST_GUILD_ID) в .env\n")
        return 2

    class ProbeBot(Conductor):
        async def setup_hook(self) -> None:
            sys.stderr.write("live_probe: setup_hook start\n")
            await super().setup_hook()
            sys.stderr.write("live_probe: setup_hook done\n")

        async def on_ready(self) -> None:
            sys.stderr.write(f"live_probe: on_ready user={self.user} guilds={len(self.guilds)}\n")
            await super().on_ready()
            sys.stderr.write(f"live_probe: on_ready done booted={self._booted}\n")

    bot = ProbeBot(settings)
    task = asyncio.create_task(bot.start(settings.discord_token), name="live-probe")
    try:
        report = await _probe(bot, task)
    except Exception as exc:
        sys.stderr.write(f"live_probe: {type(exc).__name__}: {exc}\n")
        return 1
    finally:
        if not bot.is_closed():
            await bot.close()
        await asyncio.gather(task, return_exceptions=True)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if report.get("ok") else 3


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
