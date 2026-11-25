"""Сверка каркаса: только чтение. Ничего не пишет и не умеет подтверждать запись."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import discord
from dotenv import load_dotenv
from sqlalchemy import select

from conductor.config import PROJECT_ROOT, Settings
from conductor.db.engine import create_engine, create_sessionmaker
from conductor.db.models import (
    AchievementDef,
    GroupHub,
    GuildAccessState,
    GuildConfig,
    GuildLayout,
    GuildPermission,
    GuildRank,
    LadderSettings,
    LfgSettings,
    ModSettings,
    Panel,
    PollSettings,
    ReputationTier,
    SelfRoleMenu,
    SelfRoleOption,
    StatsBoard,
    TicketPanel,
    VerificationSettings,
    VoiceHub,
)
from conductor.domain.access import (
    AccessSnapshot,
    PermissionRule,
    Rank,
    empty_rank_roles,
    parse_rank_list,
    parse_rank_token,
    scheme_drift_keys,
)
from conductor.domain.audit import GuildAudit, audit_guild
from conductor.domain.tickets import parse_ticket_settings

_CONFIG_IDS: tuple[tuple[str, str], ...] = (
    ("user_logs", "user_log_channel_id"),
    ("admin_logs", "admin_log_channel_id"),
    ("staff_feed", "staff_feed_channel_id"),
    ("automod", "automod_channel_id"),
    ("structure", "structure_channel_id"),
    ("role_ruby", "ruby_role_id"),
    ("role_emerald", "emerald_role_id"),
    ("role_sapphire", "sapphire_role_id"),
    ("role_amethyst", "amethyst_role_id"),
)
_RANK_KEYS = {
    Rank.SPECIAL.key: "role_special",
    Rank.CHIEF.key: "role_chief",
    Rank.SENIOR.key: "role_senior",
    Rank.WATCHER.key: "role_watcher",
}
_LADDER_ATTR = {
    "role_guest": "guest_role_id",
    "role_regular": "regular_role_id",
    "role_resident": "resident_role_id",
    "role_friend": "friend_role_id",
}
_REP_INDEX = {
    0: "role_sinner",
    1: "role_suspicious",
    2: "role_ordinary",
    3: "role_reliable",
    4: "role_respected",
    5: "role_exemplary",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Печатает привязки каркаса на пропавшие id, живые панели без сообщения, "
            "расхождение схемы прав и серверы с осиротевшим Специальным. Ничего не меняет."
        )
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="секунды на вход в Discord; без токена сверка id пропускается",
    )
    parser.add_argument(
        "--skip-discord",
        action="store_true",
        help="не входить в Discord, сверить только базу",
    )
    return parser


def _positive(value: object) -> int | None:
    if value is None:
        return None
    number = int(value)
    return number if number > 0 else None


def _put(bound: dict[str, int], key: str, value: object) -> None:
    found = _positive(value)
    if found is not None:
        bound[key] = found


def _decode_ranks(raw: str) -> frozenset[Rank]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return parse_rank_list(raw)
    if isinstance(payload, list):
        return parse_rank_list([str(item) for item in payload])
    return parse_rank_list(str(payload))


def _snapshot(
    *,
    special_ids: Sequence[int],
    ranks: Mapping[Rank, tuple[int, ...]],
    rules: Mapping[str, PermissionRule],
    ever_bound: bool,
) -> AccessSnapshot:
    locked = bool(tuple(special_ids))
    return AccessSnapshot(
        rank_roles=ranks,
        rules=rules,
        special_locked=locked,
        special_ever_bound=ever_bound,
    )


async def _bound_ids(session: Any, guild_id: int) -> dict[str, int]:
    bound: dict[str, int] = {}
    for row in (await session.scalars(select(GuildLayout).where(GuildLayout.guild_id == guild_id))).all():
        _put(bound, str(row.key), row.channel_id)
    config = await session.get(GuildConfig, guild_id)
    if config is not None:
        for key, attr in _CONFIG_IDS:
            _put(bound, key, getattr(config, attr, None))
    for row in (await session.scalars(select(GuildRank).where(GuildRank.guild_id == guild_id))).all():
        key = _RANK_KEYS.get(str(row.rank))
        if key is not None:
            _put(bound, key, row.role_id)
    verify = await session.get(VerificationSettings, guild_id)
    if verify is not None:
        _put(bound, "verify", verify.channel_id)
        _put(bound, "rules", verify.rules_channel_id)
        _put(bound, "role_verified", verify.verified_role_id)
    menus = (await session.scalars(select(SelfRoleMenu).where(SelfRoleMenu.guild_id == guild_id))).all()
    for menu in menus:
        if menu.channel_id:
            _put(bound, "roles", menu.channel_id)
            break
    for row in (await session.scalars(select(SelfRoleOption).where(SelfRoleOption.guild_id == guild_id))).all():
        tag = str(row.tag or "")
        if tag:
            _put(bound, f"self_role:{tag}", row.role_id)
    lfg = await session.get(LfgSettings, guild_id)
    if lfg is not None:
        _put(bound, "lfg", lfg.channel_id)
    poll = await session.get(PollSettings, guild_id)
    if poll is not None:
        _put(bound, "polls", poll.channel_id)
    stats = await session.get(StatsBoard, guild_id)
    if stats is not None:
        _put(bound, "stats", stats.channel_id)
    panel = await session.scalar(select(TicketPanel).where(TicketPanel.guild_id == guild_id).order_by(TicketPanel.id))
    if panel is not None:
        _put(bound, "tickets", panel.channel_id)
        settings = parse_ticket_settings(panel.settings)
        _put(bound, "ticket_archive", settings.archive_channel_id)
        _put(bound, "ticket_log", settings.log_channel_id)
        _put(bound, "cat_tickets", settings.discord_category_id)
    hub = await session.get(VoiceHub, guild_id)
    if hub is not None:
        _put(bound, "hub", hub.channel_id)
        _put(bound, "cat_rooms", hub.category_id)
    groups = await session.get(GroupHub, guild_id)
    if groups is not None:
        _put(bound, "cat_groups", groups.category_id)
    mod = await session.get(ModSettings, guild_id)
    if mod is not None:
        _put(bound, "role_mute", mod.mute_role_id)
        _put(bound, "cat_evidence", mod.evidence_category_id)
        _put(bound, "evidence_archive", mod.evidence_archive_channel_id)
    ladder = await session.get(LadderSettings, guild_id)
    if ladder is not None:
        for key, attr in _LADDER_ATTR.items():
            _put(bound, key, getattr(ladder, attr, None))
    for row in (await session.scalars(select(ReputationTier).where(ReputationTier.guild_id == guild_id))).all():
        key = _REP_INDEX.get(int(row.tier))
        if key is not None:
            _put(bound, key, row.role_id)
    for row in (await session.scalars(select(AchievementDef).where(AchievementDef.guild_id == guild_id))).all():
        _put(bound, f"ach:{row.key}:{int(row.stage)}", row.role_id)
    return bound


async def load_audits(
    sessions: Any,
    known_objects: Mapping[int, frozenset[int]] | None,
    known_roles: Mapping[int, frozenset[int]] | None,
) -> tuple[GuildAudit, ...]:
    reports: list[GuildAudit] = []
    async with sessions() as session:
        guild_ids: set[int] = set()
        for model in (GuildLayout, GuildConfig, GuildRank, GuildAccessState, Panel, GuildPermission):
            column = model.guild_id
            guild_ids.update(int(item) for item in (await session.scalars(select(column).distinct())).all())
        for guild_id in sorted(guild_ids):
            bound = await _bound_ids(session, guild_id)
            ranks = empty_rank_roles()
            grouped: dict[Rank, list[int]] = {rank: [] for rank in ranks}
            special_ids: list[int] = []
            for row in (await session.scalars(select(GuildRank).where(GuildRank.guild_id == guild_id))).all():
                try:
                    rank = parse_rank_token(row.rank)
                except ValueError:
                    continue
                if rank not in grouped:
                    continue
                grouped[rank].append(int(row.role_id))
                if rank is Rank.SPECIAL:
                    special_ids.append(int(row.role_id))
            for rank, role_ids in grouped.items():
                ranks[rank] = tuple(role_ids)
            state = await session.get(GuildAccessState, guild_id)
            ever_bound = bool(state.special_ever_bound) if state is not None else False
            rules: dict[str, PermissionRule] = {}
            perm_rows = (
                await session.scalars(select(GuildPermission).where(GuildPermission.guild_id == guild_id))
            ).all()
            for row in perm_rows:
                try:
                    allowed = _decode_ranks(row.ranks)
                except ValueError:
                    continue
                rules[str(row.key)] = PermissionRule(str(row.key), allowed)
            snapshot = _snapshot(
                special_ids=special_ids,
                ranks=ranks,
                rules=rules,
                ever_bound=ever_bound,
            )
            panels = [
                (str(row.kind), int(row.channel_id), int(row.message_id), str(row.state))
                for row in (await session.scalars(select(Panel).where(Panel.guild_id == guild_id))).all()
            ]
            objects = None if known_objects is None else known_objects.get(guild_id, frozenset())
            role_ids = None if known_roles is None else known_roles.get(guild_id, frozenset())
            reports.append(
                audit_guild(
                    guild_id=guild_id,
                    bound=bound,
                    live_panels=panels,
                    scheme_drift=scheme_drift_keys(snapshot),
                    special_ever_bound=ever_bound,
                    special_role_ids=special_ids,
                    known_object_ids=objects,
                    known_role_ids=role_ids,
                )
            )
        await session.rollback()
    return tuple(reports)


async def fetch_discord_ids(
    token: str, timeout: float
) -> tuple[dict[int, frozenset[int]], dict[int, frozenset[int]]] | None:
    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)
    try:
        await asyncio.wait_for(client.login(token), timeout=min(10.0, timeout))
        task = asyncio.create_task(client.connect(reconnect=False))
        try:
            await asyncio.wait_for(client.wait_until_ready(), timeout=timeout)
            objects: dict[int, frozenset[int]] = {}
            roles: dict[int, frozenset[int]] = {}
            for guild in client.guilds:
                channel_ids = {int(channel.id) for channel in guild.channels}
                role_ids = {int(role.id) for role in guild.roles}
                objects[int(guild.id)] = frozenset(channel_ids | role_ids)
                roles[int(guild.id)] = frozenset(role_ids)
            return objects, roles
        finally:
            await client.close()
            await asyncio.gather(task, return_exceptions=True)
    except (TimeoutError, discord.DiscordException, OSError) as exc:
        sys.stderr.write(f"Discord не проверен: {exc}\n")
        return None


def _print_report(report: GuildAudit, *, discord_checked: bool) -> None:
    sys.stdout.write(f"Сервер {report.guild_id}\n")
    if report.orphaned_special:
        sys.stdout.write("  осиротевший Special: ДА — ЗАБЛОКИРОВАН\n")
    elif report.special_ever_bound:
        sys.stdout.write("  осиротевший Special: нет (привязка на месте)\n")
    else:
        sys.stdout.write("  осиротевший Special: нет (ещё не выдавался)\n")
    if report.missing_ids:
        parts = ", ".join(f"{item.key}={item.object_id}" for item in report.missing_ids)
        sys.stdout.write(f"  пропавшие привязки: {parts}\n")
    elif discord_checked:
        sys.stdout.write("  пропавшие привязки: нет\n")
    else:
        sys.stdout.write("  пропавшие привязки: Discord не опрошен\n")
    if report.live_without_message:
        parts = ", ".join(f"{item.kind} message_id={item.message_id}" for item in report.live_without_message)
        sys.stdout.write(f"  живые панели без сообщения: {parts}\n")
    else:
        sys.stdout.write("  живые панели без сообщения: нет\n")
    if report.scheme_drift:
        sys.stdout.write(f"  расхождение схемы прав: {', '.join(report.scheme_drift)}\n")
    else:
        sys.stdout.write("  расхождение схемы прав: нет\n")


async def run(settings: Settings, *, timeout: float, skip_discord: bool) -> int:
    engine = create_engine(settings.database_url)
    sessions = create_sessionmaker(engine)
    known_objects: dict[int, frozenset[int]] | None = None
    known_roles: dict[int, frozenset[int]] | None = None
    discord_checked = False
    try:
        if not skip_discord and settings.token_looks_real:
            fetched = await fetch_discord_ids(settings.discord_token, timeout)
            if fetched is not None:
                known_objects, known_roles = fetched
                discord_checked = True
        elif skip_discord:
            sys.stdout.write("Discord пропущен по флагу.\n")
        else:
            sys.stdout.write("Токен не задан — сверка id в Discord пропущена.\n")
        reports = await load_audits(sessions, known_objects, known_roles)
    finally:
        await engine.dispose()

    if not reports:
        sys.stdout.write("Гильдий в базе нет.\n")
        return 0
    blocked = 0
    for report in reports:
        _print_report(report, discord_checked=discord_checked)
        if report.orphaned_special or report.missing_ids or report.live_without_message:
            blocked += 1
    sys.stdout.write(f"Всего серверов: {len(reports)}. С замечаниями: {blocked}.\n")
    return 1 if blocked else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings()
    sys.stdout.write(
        "Сверка каркаса: только чтение. Ничего не меняю. "
        f"Режим {settings.mode_label}. База {settings.database_url}.\n"
    )
    return asyncio.run(run(settings, timeout=float(args.timeout), skip_discord=bool(args.skip_discord)))


if __name__ == "__main__":
    raise SystemExit(main())
