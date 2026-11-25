"""Журнал критических сбоев: structlog и карточка в лог администрации."""

from __future__ import annotations

import time
from typing import Any

import discord
import structlog

from conductor.domain.access import Rank, resolve_rank
from conductor.domain.bugs import BugMatch
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
from conductor.domain.permissions import REQUIRED_PERMISSIONS
from conductor.presentation import copy
from conductor.presentation.errors import critical_error_card
from conductor.presentation.layout import command_failure, error_code, respond
from conductor.presentation.persistent import parse_custom_id
from conductor.presentation.text import CHANNEL_TYPE_LABELS

log = structlog.get_logger("conductor.errors")

__all__ = ["CriticalLog"]

_POST_COOLDOWN = 30.0
_POST_CACHE_LIMIT = 256
_MATCH_TTL = 24 * 3600.0


class CriticalLog:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._posted: dict[tuple[object, ...], float] = {}
        self._recent: list[tuple[float, CriticalError, str]] = []

    async def report_interaction(
        self,
        interaction: discord.Interaction,
        error: BaseException,
        *,
        notify_user: bool = False,
        item: object | None = None,
    ) -> CriticalError | None:
        try:
            if not is_critical_error(error):
                return None
            report = await self._from_interaction(interaction, error, item=item)
            self._emit(report, error)
            self._remember(report, self._jump(report))
            self._post(report)
            if notify_user:
                await self._notify(interaction)
            return report
        except Exception:
            log.exception("critical_report_failed", error=repr(error))
            if notify_user:
                await self._notify(interaction)
            return None

    async def report_listener(self, event: str, error: BaseException, *args: object) -> CriticalError | None:
        try:
            if not is_critical_error(error):
                return None
            report = self._from_listener(event, error, args)
            self._emit(report, error)
            self._remember(report, self._jump(report))
            self._post(report)
            return report
        except Exception:
            log.exception("critical_listener_report_failed", event=event, error=repr(error))
            return None

    async def report_task(self, name: str, error: BaseException) -> CriticalError | None:
        try:
            if not is_critical_error(error):
                return None
            inner = unwrap_error(error)
            report = CriticalError(
                code=error_code_of(id(error)),
                kind=ErrorKind.TASK,
                action=name,
                error_type=type(inner).__name__,
                error_text=sanitize_error_text(str(inner)),
                origin=error_origin(error),
            )
            self._emit(report, error)
            self._remember(report)
            return report
        except Exception:
            log.exception("critical_task_report_failed", task=name, error=repr(error))
            return None

    async def _from_interaction(
        self,
        interaction: discord.Interaction,
        error: BaseException,
        *,
        item: object | None,
    ) -> CriticalError:
        inner = unwrap_error(error)
        guild = getattr(interaction, "guild", None)
        channel = getattr(interaction, "channel", None)
        user = getattr(interaction, "user", None)
        perms = getattr(user, "guild_permissions", None)
        owner_id = getattr(guild, "owner_id", None)
        user_id = _snowflake(user)
        rank = await self._rank(guild, user)
        try:
            code = error_code(interaction)
        except Exception:
            code = error_code_of(user_id or id(error))
        return CriticalError(
            code=code,
            kind=_kind_of(interaction),
            action=_action_of(interaction, item),
            error_type=type(inner).__name__,
            error_text=sanitize_error_text(str(inner)),
            origin=error_origin(error),
            guild_id=_snowflake(guild),
            guild_name=str(getattr(guild, "name", "") or ""),
            channel_id=_snowflake(channel),
            channel_name=_channel_name(channel),
            channel_kind=_channel_kind(channel),
            user_id=user_id,
            user_name=str(getattr(user, "display_name", None) or getattr(user, "name", "") or ""),
            is_developer=_is_developer(self._bot, user_id),
            is_guild_owner=bool(user_id and owner_id is not None and _as_int(owner_id) == user_id),
            is_administrator=bool(getattr(perms, "administrator", False)),
            rank=rank,
            discord_perms=held_permission_attrs(perms),
            missing_bot_perms=_missing_bot_perms(guild),
            extras=_extras_of(interaction),
        )

    def _from_listener(self, event: str, error: BaseException, args: tuple[object, ...]) -> CriticalError:
        inner = unwrap_error(error)
        guild = _guild_from(args)
        user = _user_from(args)
        channel = _channel_from(args)
        perms = getattr(user, "guild_permissions", None)
        user_id = _snowflake(user)
        owner_id = getattr(guild, "owner_id", None)
        return CriticalError(
            code=error_code_of(id(error)),
            kind=ErrorKind.LISTENER,
            action=event,
            error_type=type(inner).__name__,
            error_text=sanitize_error_text(str(inner)),
            origin=error_origin(error),
            guild_id=_snowflake(guild),
            guild_name=str(getattr(guild, "name", "") or ""),
            channel_id=_snowflake(channel),
            channel_name=_channel_name(channel),
            channel_kind=_channel_kind(channel),
            user_id=user_id,
            user_name=str(getattr(user, "display_name", None) or getattr(user, "name", "") or ""),
            is_developer=_is_developer(self._bot, user_id),
            is_guild_owner=bool(user_id and owner_id is not None and _as_int(owner_id) == user_id),
            is_administrator=bool(getattr(perms, "administrator", False)),
            discord_perms=held_permission_attrs(perms),
            missing_bot_perms=_missing_bot_perms(guild),
        )

    async def _rank(self, guild: object | None, user: object) -> str:
        if guild is None:
            return Rank.NONE.key
        try:
            from conductor.services.access import cached_snapshot

            snapshot = await cached_snapshot(self._bot, int(getattr(guild, "id", 0) or 0))
        except Exception:
            return Rank.NONE.key
        roles = tuple(int(role.id) for role in getattr(user, "roles", ()) or () if getattr(role, "id", None))
        return resolve_rank(roles, snapshot.rank_roles).key

    def lookup(self, guild_id: int | None, code: str) -> BugMatch | None:
        """Запись CriticalLog за 24 часа с тем же кодом E-XXXX."""
        wanted = str(code or "").strip().upper()
        if not wanted:
            return None
        self._prune_recent()
        for _stamp, report, jump in reversed(self._recent):
            if str(report.code or "").upper() != wanted:
                continue
            if guild_id is not None and report.guild_id not in (None, int(guild_id)):
                continue
            return BugMatch(
                code=report.code,
                error_type=report.error_type,
                origin=report.origin,
                jump_url=jump,
            )
        return None

    def _remember(self, report: CriticalError, jump: str = "") -> None:
        now = time.monotonic()
        self._recent.append((now, report, jump))
        self._prune_recent(now)

    def _prune_recent(self, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else now
        cutoff = stamp - _MATCH_TTL
        if any(item[0] < cutoff for item in self._recent):
            self._recent = [item for item in self._recent if item[0] >= cutoff]

    def _jump(self, report: CriticalError) -> str:
        if report.guild_id is None:
            return ""
        mapping = getattr(self._bot, "config_by_guild", None)
        getter = getattr(mapping, "get", None)
        if not callable(getter):
            return ""
        config = getter(int(report.guild_id))
        channel_id = getattr(config, "admin_log_channel_id", None)
        if not channel_id:
            return ""
        return f"https://discord.com/channels/{int(report.guild_id)}/{int(channel_id)}"

    def _emit(self, report: CriticalError, error: BaseException) -> None:
        try:
            log.error(
                "critical_error",
                code=report.code,
                kind=report.kind,
                action=report.action,
                extras=report.extras or None,
                error_type=report.error_type,
                error=report.error_text,
                origin=report.origin,
                guild_id=report.guild_id,
                guild=report.guild_name or None,
                channel_id=report.channel_id,
                channel=report.channel_name or None,
                user_id=report.user_id,
                user=report.user_name or None,
                developer=report.is_developer,
                guild_owner=report.is_guild_owner,
                administrator=report.is_administrator,
                rank=report.rank,
                discord_perms=list(report.discord_perms),
                missing_bot_perms=list(report.missing_bot_perms),
                exc_info=(type(error), error, getattr(error, "__traceback__", None)),
            )
        except Exception:
            log.error("critical_error", code=report.code, error=repr(error))

    def _post(self, report: CriticalError) -> None:
        try:
            if report.guild_id is None or self._throttled(report):
                return
            mapping = getattr(self._bot, "config_by_guild", None)
            getter = getattr(mapping, "get", None)
            if not callable(getter):
                return
            config = getter(int(report.guild_id))
            channel_id = getattr(config, "admin_log_channel_id", None)
            if not channel_id:
                return
            pump = getattr(self._bot, "log_pump", None)
            submit = getattr(pump, "submit", None)
            if not callable(submit):
                return
            submit(int(channel_id), view=critical_error_card(report))
        except Exception:
            log.exception("critical_card_failed", code=report.code)

    def _throttled(self, report: CriticalError) -> bool:
        key = (report.guild_id, report.kind, report.action, report.error_type)
        now = time.monotonic()
        last = self._posted.get(key, 0.0)
        if now - last < _POST_COOLDOWN:
            return True
        self._posted[key] = now
        if len(self._posted) > _POST_CACHE_LIMIT:
            cutoff = now - _POST_COOLDOWN
            self._posted = {item: stamp for item, stamp in self._posted.items() if stamp >= cutoff}
        return False

    async def _notify(self, interaction: discord.Interaction) -> None:
        try:
            await respond(interaction, command_failure(interaction))
        except Exception:
            log.warning("critical_notify_unsent", code=error_code(interaction))


def _kind_of(interaction: discord.Interaction) -> ErrorKind:
    kind = getattr(interaction, "type", None)
    name = getattr(kind, "name", "") or str(kind or "")
    if name == "modal_submit":
        return ErrorKind.MODAL
    if name in {"component", "autocomplete"}:
        return ErrorKind.COMPONENT
    return ErrorKind.COMMAND


def _action_of(interaction: discord.Interaction, item: object | None) -> str:
    command = getattr(interaction, "command", None)
    name = getattr(command, "qualified_name", None)
    if name:
        return copy.error_slash(str(name))
    custom = _custom_id(interaction, item)
    parsed = parse_custom_id(custom) if custom else None
    if parsed is not None:
        return f"{parsed.kind}:{parsed.action}"
    if custom:
        return custom[:80]
    return copy.error_kind_label(_kind_of(interaction))


def _extras_of(interaction: discord.Interaction) -> str:
    bits: list[str] = []
    namespace = getattr(interaction, "namespace", None)
    if namespace is not None:
        try:
            items = vars(namespace).items()
        except TypeError:
            items = ()
        for key, value in items:
            if key.startswith("_") or value is None:
                continue
            bits.append(f"{key}={_short(value)}")
    custom = _custom_id(interaction, None)
    parsed = parse_custom_id(custom) if custom else None
    if parsed is not None:
        bits.append(f"ref={parsed.ref}")
    return " · ".join(bits[:8])


def _custom_id(interaction: discord.Interaction, item: object | None) -> str:
    raw = getattr(item, "custom_id", None)
    if raw:
        return str(raw)
    data = getattr(interaction, "data", None)
    if isinstance(data, dict):
        found = data.get("custom_id")
        if found:
            return str(found)
    return ""


def _missing_bot_perms(guild: object | None) -> tuple[str, ...]:
    me = getattr(guild, "me", None)
    perms = getattr(me, "guild_permissions", None)
    if perms is None:
        return ()
    return tuple(item.attr for item in REQUIRED_PERMISSIONS if not bool(getattr(perms, item.attr, False)))


def _channel_name(channel: object | None) -> str:
    return str(getattr(channel, "name", "") or "")


def _channel_kind(channel: object | None) -> str:
    raw = getattr(getattr(channel, "type", None), "name", "") or ""
    return CHANNEL_TYPE_LABELS.get(raw, raw)


def _is_developer(bot: Any, user_id: int | None) -> bool:
    if not user_id:
        return False
    probe = getattr(bot, "is_developer", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(user_id))
    except Exception:
        return False


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _snowflake(obj: object | None) -> int | None:
    if obj is None:
        return None
    return _as_int(getattr(obj, "id", None))


def _guild_from(args: tuple[object, ...]) -> object | None:
    for arg in args:
        if arg is None:
            continue
        guild = getattr(arg, "guild", None)
        if guild is not None:
            return guild
        if type(arg).__name__ == "Guild":
            return arg
    return None


def _user_from(args: tuple[object, ...]) -> object | None:
    for arg in args:
        if arg is None:
            continue
        author = getattr(arg, "author", None) or getattr(arg, "user", None)
        if author is not None:
            return author
        if type(arg).__name__ in {"Member", "User"}:
            return arg
    return None


def _channel_from(args: tuple[object, ...]) -> object | None:
    for arg in args:
        if arg is None:
            continue
        channel = getattr(arg, "channel", None)
        if channel is not None:
            return channel
        if (
            getattr(arg, "guild", None) is not None
            and getattr(arg, "name", None) is not None
            and type(arg).__name__ != "Guild"
        ):
            return arg
    return None


def _short(value: object) -> str:
    if hasattr(value, "id") and hasattr(value, "name"):
        return str(getattr(value, "name", "") or value.id)
    text = str(value)
    return text if len(text) <= 40 else text[:39] + "…"
