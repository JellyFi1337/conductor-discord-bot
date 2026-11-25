"""Исполнение автомодерации: удаление, дело, hit, карточки."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

import discord
import structlog
from sqlalchemy import func, select

from conductor.db.models import AutomodHit, AutomodRequest, MemberRecord, Panel
from conductor.db.session import session_scope
from conductor.domain.access import Rank, resolve_rank
from conductor.domain.automod import (
    AUTOMOD_RULES,
    EDIT_RULES,
    AutomodAction,
    AutomodReason,
    DeleteMode,
    ExemptCause,
    MessageFacts,
    RequestStatus,
    Trigger,
    card_fingerprint,
    count_role_mentions,
    count_user_mentions,
    default_config,
    exempt_cause,
    extract_urls,
    foreign_invites,
    has_everyone,
    invite_codes,
    is_newcomer,
    may_decide_automod,
    normalize_for_duplicate,
    rule_exempt,
    rule_specs,
    sanitize_request,
)
from conductor.domain.moderation import CaseKind
from conductor.domain.staff_feed import ACTION_AUTOMOD, ACTION_WATCH_HIT
from conductor.domain.traffic import Lane
from conductor.presentation import copy
from conductor.presentation.automod import PANEL_KIND, hit_card, request_card, rules_card
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import denied, edit_card, panel, send_card
from conductor.presentation.persistent import PANEL_LIVE
from conductor.presentation.tokens import Tone
from conductor.services.access import cached_snapshot, log_limit_bypass
from conductor.services.history import skip_ids_from_config
from conductor.services.panels import find_live_kind, find_usable_kind, mark_live, mark_stale
from conductor.services.runtime import log_tick
from conductor.services.staff_feed import emit_staff
from conductor.services.traffic import lane

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.automod")

__all__ = [
    "AutomodService",
    "collect_facts",
    "facts_from_edit",
]


_INVITE_TTL = 3600.0


def collect_facts(message: discord.Message, *, member_since: datetime | None = None) -> MessageFacts | None:
    guild = message.guild
    author = message.author
    if guild is None or getattr(author, "bot", False) or getattr(message, "webhook_id", None):
        return None
    names = tuple(str(getattr(item, "filename", "") or "") for item in getattr(message, "attachments", ()) or ())
    text = _combined_text(message)
    urls = [*extract_urls(text)]
    for embed in getattr(message, "embeds", ()) or ():
        for candidate in (getattr(embed, "url", None), getattr(getattr(embed, "thumbnail", None), "url", None)):
            if candidate:
                urls.append(str(candidate))
    mentions = tuple(getattr(message, "mentions", ()) or ())
    roles = tuple(getattr(message, "role_mentions", ()) or ())
    everyone_mark = f"<@&{int(guild.id)}>"
    everyone = bool(getattr(message, "mention_everyone", False) or has_everyone(text) or everyone_mark in text)
    return MessageFacts(
        guild_id=int(guild.id),
        user_id=int(author.id),
        channel_id=int(message.channel.id),
        message_id=int(message.id),
        text=text,
        attachment_names=names,
        urls=tuple(urls),
        user_mentions=len(mentions),
        role_mentions=len(roles),
        member_since=member_since or getattr(author, "joined_at", None),
        everyone=everyone,
    )


def facts_from_edit(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    user_id: int,
    content: str,
    member_since: datetime | None = None,
    mention_everyone: bool = False,
) -> MessageFacts:
    text = str(content or "")
    everyone_mark = f"<@&{int(guild_id)}>"
    everyone = bool(mention_everyone or has_everyone(text) or everyone_mark in text)
    return MessageFacts(
        guild_id=int(guild_id),
        user_id=int(user_id),
        channel_id=int(channel_id),
        message_id=int(message_id),
        text=text,
        urls=extract_urls(text),
        user_mentions=count_user_mentions(text),
        role_mentions=count_role_mentions(text),
        member_since=member_since,
        everyone=everyone,
    )


def _combined_text(message: object) -> str:
    parts = [str(getattr(message, "content", "") or "")]
    parts.extend(_snapshot_texts(getattr(message, "message_snapshots", None)))
    return "\n".join(part for part in parts if part)


def _snapshot_texts(snaps: object) -> list[str]:
    found: list[str] = []
    for snap in snaps or ():
        text = str(snap.get("content") or "") if isinstance(snap, dict) else str(getattr(snap, "content", "") or "")
        if text:
            found.append(text)
    return found


def _payload_text(data: Mapping[str, Any] | object) -> str:
    if isinstance(data, dict):
        parts = [str(data.get("content") or "")]
        parts.extend(_snapshot_texts(data.get("message_snapshots")))
        return "\n".join(part for part in parts if part)
    return _combined_text(data)


class AutomodService:
    def __init__(self, bot: Conductor) -> None:
        self._bot = bot
        self._card_hash: dict[int, str] = {}
        self._invite_guild: dict[str, tuple[float, int | None, bool]] = {}
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(3600, "automod-card", self.refresh_cards)

    def enqueue(self, facts: MessageFacts, *, only: tuple[str, ...] | None = None) -> None:
        key = (int(facts.guild_id), int(facts.user_id))
        queue = getattr(self._bot, "queue", None)
        submit = getattr(queue, "submit", None)
        if callable(submit):
            submit(key, lambda: self.handle(facts, only=only))
            return
        spawn = getattr(self._bot, "spawn", None)
        if callable(spawn):
            spawn(self.handle(facts, only=only), name=f"automod-{key[0]}-{key[1]}")

    async def inspect(self, message: discord.Message) -> bool:
        if not self._eligible_message(message):
            return False
        if await self._log_channel(message.guild, message.channel.id):
            return True
        facts = collect_facts(message)
        if facts is None:
            return False
        wanted = await self._wanted_rules(message.guild, message.author, message.channel.id, None)
        if not wanted:
            return True
        self.enqueue(facts)
        return True

    async def inspect_edit(self, payload: discord.RawMessageUpdateEvent) -> bool:
        guild_id = payload.guild_id
        if guild_id is None or not getattr(self._bot, "serves", lambda _gid: True)(int(guild_id)):
            return False
        data = getattr(payload, "data", {}) or {}
        if isinstance(data, dict) and "content" not in data and "message_snapshots" not in data:
            return False
        author = data.get("author") or {} if isinstance(data, dict) else {}
        if bool(author.get("bot")):
            return False
        try:
            user_id = int(author.get("id") or 0)
        except (TypeError, ValueError):
            return False
        if not user_id:
            return False
        content = _payload_text(data)
        cached = getattr(payload, "cached_message", None)
        if cached is not None and _combined_text(cached) == content:
            return False
        guild = self._guild(int(guild_id))
        if guild is None:
            return False
        member = guild.get_member(user_id)
        if member is None or getattr(member, "bot", False):
            return False
        if await self._log_channel(guild, int(payload.channel_id)):
            return True
        mention_everyone = bool(data.get("mention_everyone")) if isinstance(data, dict) else False
        self.enqueue(
            facts_from_edit(
                guild_id=int(guild_id),
                channel_id=int(payload.channel_id),
                message_id=int(payload.message_id),
                user_id=user_id,
                content=content,
                member_since=getattr(member, "joined_at", None),
                mention_everyone=mention_everyone,
            ),
            only=EDIT_RULES,
        )
        return True

    async def handle(
        self,
        facts: MessageFacts,
        *,
        only: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> list[Trigger]:
        moment = now if now is not None else datetime.now(UTC)
        facts = await self._with_tenure(facts, now=moment)
        guild = self._guild(facts.guild_id)
        member = None if guild is None else guild.get_member(facts.user_id)
        facts = await self._classify_invites(facts, guild)
        wanted = await self._wanted_rules(guild, member, facts.channel_id, only)
        if not wanted:
            return []
        rules = getattr(self._bot, "automod_rules", None)
        if rules is None:
            return []
        triggers = rules.evaluate(facts, now=moment, only=wanted)
        if not triggers:
            return []
        channel = self._channel(guild, facts.channel_id)
        for trigger in triggers:
            await self._execute(guild, member, channel, facts, trigger, moment)
            text = normalize_for_duplicate(facts.text) if trigger.rule == "duplicate" else ""
            rules.after_trigger(facts, trigger.rule, text=text)
        return triggers

    def _eligible_message(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or not getattr(self._bot, "serves", lambda _gid: True)(guild.id):
            return False
        return not (getattr(message.author, "bot", False) or getattr(message, "webhook_id", None))

    async def _log_channel(self, guild: object, channel_id: int) -> bool:
        rules = getattr(self._bot, "automod_rules", None)
        config = getattr(rules, "config", None)
        if config is None:
            return True
        guild_id = int(getattr(guild, "id", 0) or 0)
        logs = skip_ids_from_config(getattr(self._bot, "config_by_guild", {}).get(guild_id))
        return int(channel_id) in {int(item) for item in logs}

    async def _wanted_rules(
        self,
        guild: object | None,
        member: object | None,
        channel_id: int,
        only: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        rules = getattr(self._bot, "automod_rules", None)
        config = getattr(rules, "config", None)
        if config is None or guild is None or member is None:
            return ()
        guild_id = int(getattr(guild, "id", 0) or 0)
        if await self._log_channel(guild, channel_id):
            return ()
        snapshot = await cached_snapshot(self._bot, guild_id)
        role_ids = tuple(int(role.id) for role in getattr(member, "roles", ()) or ())
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        is_admin = bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))
        is_owner = int(getattr(guild, "owner_id", 0) or 0) == int(getattr(member, "id", 0) or 0)
        wanted: list[str] = []
        logged_limits = False
        for name in AUTOMOD_RULES:
            if only is not None and name not in only:
                continue
            rule = config.rules.get(name)
            if rule is None or rule.action is AutomodAction.OFF:
                continue
            cause = exempt_cause(
                channel_id=int(channel_id),
                rank=rank.key,
                role_ids=role_ids,
                is_administrator=is_admin,
                is_guild_owner=is_owner,
                config=config,
                exempt=rule_exempt(rule, config),
            )
            if cause is None:
                wanted.append(name)
                continue
            log.info(
                "automod_exempt",
                guild_id=guild_id,
                user_id=int(getattr(member, "id", 0) or 0),
                rule=name,
                cause=cause.value,
            )
            if cause is ExemptCause.LIMITS and not logged_limits:
                log_limit_bypass(guild_id, int(getattr(member, "id", 0) or 0), copy.AUTOMOD_BYPASS, bot=self._bot)
                logged_limits = True
        return tuple(wanted)

    async def _execute(
        self,
        guild: discord.Guild | None,
        member: discord.Member | None,
        channel: Any,
        facts: MessageFacts,
        trigger: Trigger,
        now: datetime,
    ) -> None:
        rules = self._bot.automod_rules
        state = rules.state_for(facts.guild_id, facts.user_id)
        dup_text = normalize_for_duplicate(facts.text) if trigger.rule == "duplicate" else None
        ids = state.message_ids(trigger.rule, text=dup_text)
        reason = self._reason(facts, trigger, channel, now)
        await self._delete(channel, facts, trigger, ids)
        case_id: int | None = None
        window = rules.config.window
        if self._should_case(trigger, state, member, now, window) and guild is not None and member is not None:
            case_id = await self._apply(guild, member, trigger, now, reason.audit)
            if case_id is not None:
                state.mark_cased(trigger.rule, now)
        await rules.record_hit(
            guild_id=facts.guild_id,
            user_id=facts.user_id,
            rule=trigger.rule,
            channel_id=facts.channel_id,
            message_id=facts.message_id,
            case_id=case_id,
            now=now,
        )
        self._log_hit(facts, trigger, case_id, reason)
        await emit_staff(
            self._bot,
            facts.guild_id,
            actor_id=self._bot_id(),
            actor_rank=Rank.SPECIAL,
            action=ACTION_AUTOMOD,
            target=facts.user_id,
            ref=case_id,
            details=reason.staff,
        )
        await self._dm_member(member, reason)
        if case_id is None:
            await self._watch_hit(facts)

    def _reason(
        self,
        facts: MessageFacts,
        trigger: Trigger,
        channel: Any,
        now: datetime,
    ) -> AutomodReason:
        until = None
        if trigger.action is AutomodAction.TIMEOUT:
            until = now + timedelta(minutes=int(trigger.timeout_minutes))
        channel_name = str(getattr(channel, "name", "") or facts.channel_id)
        window = self._bot.automod_rules.config.window_minutes
        return copy.automod_reason(
            trigger,
            facts,
            rule_title=copy.automod_rule_title(trigger.rule),
            channel_name=channel_name,
            until=until,
            ticket_hint=copy.AUTOMOD_TICKET_HINT,
            fail_closed=copy.AUTOMOD_INVITE_FAIL_CLOSED if facts.invite_fail_closed else "",
            window_minutes=window,
        )

    def _should_case(self, trigger: Trigger, state: Any, member: object | None, now: datetime, window: Any) -> bool:
        if trigger.action is AutomodAction.DELETE_ONLY:
            return False
        if state.was_cased(trigger.rule, now, window):
            return False
        return not (trigger.action is AutomodAction.TIMEOUT and _timeout_active(member, now))

    async def _apply(
        self,
        guild: discord.Guild,
        member: discord.Member,
        trigger: Trigger,
        now: datetime,
        reason: str,
    ) -> int | None:
        moderation = getattr(self._bot, "moderation", None)
        apply = getattr(moderation, "apply", None)
        actor = getattr(guild, "me", None)
        if not callable(apply) or actor is None:
            return None
        kind = CaseKind.TIMEOUT if trigger.action is AutomodAction.TIMEOUT else CaseKind.WARN
        duration = None if kind is CaseKind.WARN else copy.minutes_short(trigger.timeout_minutes)
        outcome = await apply(
            guild,
            actor,
            member,
            kind=kind,
            reason=reason,
            duration_raw=duration,
            now=now,
            system=True,
        )
        case = getattr(outcome, "case", None)
        return None if case is None else int(case.id)

    async def _delete(self, channel: Any, facts: MessageFacts, trigger: Trigger, ids: Sequence[int]) -> None:
        if trigger.delete is DeleteMode.NONE or channel is None:
            return
        if trigger.delete is DeleteMode.MESSAGE:
            await self._delete_one(channel, facts.message_id)
            return
        keep = ids[0] if ids else facts.message_id
        drop = [mid for mid in ids if mid and mid != keep]
        if not drop:
            await self._delete_one(channel, facts.message_id)
            return
        if len(drop) == 1:
            await self._delete_one(channel, drop[0])
            return
        purge = getattr(channel, "delete_messages", None)
        if not callable(purge):
            for mid in drop:
                await self._delete_one(channel, mid)
            return
        try:
            await self._discord(purge([discord.Object(id=mid) for mid in drop]))
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            log.warning("automod_bulk_delete_failed", channel_id=facts.channel_id, error=str(exc))

    async def _delete_one(self, channel: Any, message_id: int) -> None:
        getter = getattr(channel, "get_partial_message", None)
        target = getter(int(message_id)) if callable(getter) else None
        delete = getattr(target, "delete", None) if target is not None else None
        if not callable(delete):
            return
        try:
            await self._discord(delete())
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            log.warning("automod_delete_failed", message_id=message_id, error=str(exc))

    def _log_hit(self, facts: MessageFacts, trigger: Trigger, case_id: int | None, reason: AutomodReason) -> None:
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        config = getattr(self._bot, "config_by_guild", {}).get(facts.guild_id)
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        if not callable(submit) or channel_id is None:
            return
        submit(
            int(channel_id),
            view=hit_card(
                trigger,
                user_id=facts.user_id,
                channel_id=facts.channel_id,
                window_minutes=self._bot.automod_rules.config.window_minutes,
                case_id=case_id,
                staff=reason.staff,
            ),
        )

    async def _watch_hit(self, facts: MessageFacts) -> None:
        moderation = getattr(self._bot, "moderation", None)
        lookup = getattr(moderation, "active_watch", None)
        if not callable(lookup):
            return
        watch = await lookup(facts.guild_id, facts.user_id)
        if watch is None:
            return
        await emit_staff(
            self._bot,
            facts.guild_id,
            actor_id=self._bot_id(),
            actor_rank=Rank.SPECIAL,
            action=ACTION_WATCH_HIT,
            target=facts.user_id,
            details=getattr(watch, "reason", None),
        )

    async def _with_tenure(self, facts: MessageFacts, *, now: datetime) -> MessageFacts:
        if not self._tenure_needed(facts, now):
            return facts
        key = (int(facts.guild_id), int(facts.user_id))
        cache = getattr(getattr(self._bot, "caches", None), "member_joined", None)
        if cache is not None:
            cached = cache.get(key)
            if cached is not None or cache.has(key):
                return self._with_joined(facts, cached)
        joined = await self._load_first_joined(facts.guild_id, facts.user_id)
        if cache is not None:
            cache.set(key, joined)
        return self._with_joined(facts, joined)

    def _tenure_needed(self, facts: MessageFacts, now: datetime) -> bool:
        rules = getattr(self._bot, "automod_rules", None)
        config = getattr(rules, "config", None)
        newcomer = getattr(config, "newcomer", None)
        if newcomer is None:
            return False
        if getattr(newcomer, "days", 1) == 0 or getattr(newcomer, "factor", 0) == 1:
            return False
        hours = int(getattr(newcomer, "hours", 0) or 0)
        if facts.member_since is None:
            return True
        return is_newcomer(facts.member_since, now, hours)

    def _with_joined(self, facts: MessageFacts, joined: datetime | None) -> MessageFacts:
        if joined is None:
            return facts
        return replace(facts, member_since=joined)

    async def _classify_invites(self, facts: MessageFacts, guild: discord.Guild | None) -> MessageFacts:
        codes = invite_codes(facts.text)
        if not codes or guild is None:
            return replace(facts, foreign_invites=0, foreign_codes=(), invite_fail_closed=False)
        allowed: set[str] = set()
        vanity = getattr(guild, "vanity_url_code", None)
        if vanity:
            allowed.add(str(vanity).casefold())
        tracker = getattr(self._bot, "invites", None)
        lookup = getattr(tracker, "codes_for", None)
        if callable(lookup):
            allowed.update(str(item).casefold() for item in lookup(int(guild.id)))
        pending = foreign_invites(codes, allowed)
        foreign: list[str] = []
        fail_closed = False
        for code in pending:
            guild_id, failed = await self._invite_guild_id(code)
            if failed:
                fail_closed = True
                foreign.append(code)
                continue
            if guild_id is not None and int(guild_id) == int(guild.id):
                allowed.add(code.casefold())
                continue
            foreign.append(code)
        return replace(
            facts,
            foreign_invites=len(foreign),
            foreign_codes=tuple(foreign),
            invite_fail_closed=fail_closed,
        )

    async def _invite_guild_id(self, code: str) -> tuple[int | None, bool]:
        key = str(code).casefold()
        now = monotonic()
        cached = self._invite_guild.get(key)
        if cached is not None and cached[0] > now:
            return cached[1], cached[2]
        fetch = getattr(self._bot, "fetch_invite", None)
        if not callable(fetch):
            self._invite_guild[key] = (now + _INVITE_TTL, None, True)
            return None, True
        try:
            with lane(Lane.BACKGROUND):
                invite = await fetch(code)
        except (discord.NotFound, discord.HTTPException, OSError, ValueError, TypeError):
            self._invite_guild[key] = (now + _INVITE_TTL, None, True)
            return None, True
        guild = getattr(invite, "guild", None)
        guild_id = getattr(guild, "id", None)
        resolved = int(guild_id) if guild_id is not None else None
        self._invite_guild[key] = (now + _INVITE_TTL, resolved, False)
        return resolved, False

    async def _dm_member(self, member: object | None, reason: AutomodReason) -> None:
        if member is None:
            return
        view = panel(copy.AUTOMOD_HIT_TITLE, reason.member, tone=Tone.WARN)
        try:
            await self._discord(send_card(member, view, mentions=discord.AllowedMentions.none()))
        except discord.Forbidden:
            return
        except discord.HTTPException as exc:
            log.warning("automod_member_dm_failed", user_id=getattr(member, "id", None), error=str(exc))

    async def _load_first_joined(self, guild_id: int, user_id: int) -> datetime | None:
        sessions = getattr(self._bot, "sessions", None)
        if sessions is None:
            return None
        async with session_scope(sessions) as session:
            return await session.scalar(
                select(MemberRecord.first_joined_at).where(
                    MemberRecord.guild_id == int(guild_id),
                    MemberRecord.user_id == int(user_id),
                )
            )

    def _guild(self, guild_id: int) -> discord.Guild | None:
        getter = getattr(self._bot, "get_guild", None)
        if not callable(getter):
            return None
        found = getter(int(guild_id))
        return found if found is not None else None

    def _channel(self, guild: discord.Guild | None, channel_id: int) -> Any:
        if guild is None:
            return None
        getter = getattr(guild, "get_channel", None)
        if not callable(getter):
            return None
        return getter(int(channel_id))

    def _bot_id(self) -> int:
        user = getattr(self._bot, "user", None)
        return int(getattr(user, "id", 0) or 0)

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable

    async def card_status(self, guild_id: int) -> str:
        config = getattr(self._bot, "config_by_guild", {}).get(int(guild_id))
        channel_id = None if config is None else getattr(config, "automod_channel_id", None)
        if not channel_id:
            return "off"
        async with session_scope(self._bot.sessions) as session:
            live = await find_live_kind(session, guild_id=int(guild_id), kind=PANEL_KIND)
        return "live" if live is not None else "stale"

    async def guard_panel(self, guild_id: int, ref: str) -> GuardResource | None:
        """Карточка правил: нажатие доказывает, что сообщение живо."""
        try:
            panel_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Panel, panel_id)
            if row is None:
                return GuardResource(ref=str(ref), guild_id=int(guild_id))
            if row.kind != PANEL_KIND:
                return None
            if int(row.guild_id) != int(guild_id):
                return GuardResource(ref=str(ref), guild_id=int(row.guild_id), closed=True)
            await mark_live(session, [int(row.id)])
        return GuardResource(ref=str(ref), guild_id=int(guild_id))

    async def post_card(self, guild: object, channel: object, *, actor_id: int) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        channel_id = int(getattr(channel, "id", 0) or 0)
        view, digest = await self._rules_view(guild_id, ref=0)
        async with session_scope(self._bot.sessions) as session:
            existing = await find_usable_kind(session, guild_id=guild_id, kind=PANEL_KIND)
            if existing is not None and int(existing.channel_id) == channel_id:
                panel_id = int(existing.id)
                message_id = int(existing.message_id)
            else:
                if existing is not None:
                    await mark_stale(session, [int(existing.id)])
                row = Panel(
                    kind=PANEL_KIND,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=0,
                    ref="0",
                    created_by=int(actor_id),
                    created_at=datetime.now(UTC),
                    state=PANEL_LIVE,
                )
                session.add(row)
                await session.flush()
                row.ref = str(row.id)
                row.message_id = -int(row.id)
                panel_id = int(row.id)
                message_id = 0
        view, digest = await self._rules_view(guild_id, ref=panel_id)
        if message_id > 0:
            edited = await self._edit_message(guild, channel_id, message_id, view)
            if edited:
                self._card_hash[guild_id] = digest
                async with session_scope(self._bot.sessions) as session:
                    await mark_live(session, [panel_id])
                return True
        send = getattr(channel, "send", None)
        if not callable(send):
            return False
        try:
            message = await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("automod_card_send_failed", guild_id=guild_id, error=str(exc))
            return False
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Panel, panel_id)
            if row is not None:
                row.message_id = int(message.id)
                row.channel_id = channel_id
                row.ref = str(panel_id)
                row.state = PANEL_LIVE
        self._card_hash[guild_id] = digest
        return True

    async def refresh_cards(self) -> None:
        changed = 0
        for guild in getattr(self._bot, "guilds", ()) or ():
            guild_id = int(getattr(guild, "id", 0) or 0)
            if not guild_id or not getattr(self._bot, "serves", lambda _gid: True)(guild_id):
                continue
            if await self.refresh_card(guild):
                changed += 1
        log_tick(log, "automod_card", changed=changed)

    async def refresh_card(self, guild: object) -> bool:
        guild_id = int(getattr(guild, "id", 0) or 0)
        async with session_scope(self._bot.sessions) as session:
            existing = await find_usable_kind(session, guild_id=guild_id, kind=PANEL_KIND)
            if existing is None:
                return False
            panel_id = int(existing.id)
            channel_id = int(existing.channel_id)
            message_id = int(existing.message_id)
        view, digest = await self._rules_view(guild_id, ref=panel_id)
        if self._card_hash.get(guild_id) == digest:
            return False
        if guild_id not in self._card_hash:
            self._card_hash[guild_id] = digest
            return False
        edited = await self._edit_message(guild, channel_id, message_id, view)
        if edited:
            self._card_hash[guild_id] = digest
            async with session_scope(self._bot.sessions) as session:
                await mark_live(session, [panel_id])
        return edited

    async def reload_card(self, guild: object) -> Any:
        rules = getattr(self._bot, "automod_rules", None)
        result = rules.reload() if rules is not None else None
        await self.refresh_card(guild)
        if result is not None and getattr(result, "errors", ()):
            return panel(copy.AUTOMOD_FILE_REJECTED_TITLE, copy.AUTOMOD_RELOAD_KEEP, tone=Tone.WARN)
        if result is not None and getattr(result, "missing", False):
            return panel(copy.AUTOMOD_CARD_TITLE, copy.AUTOMOD_RELOAD_MISSING, tone=Tone.INFO)
        return panel(copy.AUTOMOD_CARD_TITLE, copy.AUTOMOD_RELOAD_OK, tone=Tone.OK)

    async def submit_request(
        self,
        guild_id: int,
        *,
        author_id: int,
        rule: str,
        proposal: str,
        reason: str,
    ) -> Any:
        parsed = sanitize_request(rule=rule, proposal=proposal, reason=reason)
        if parsed is None:
            return denied(copy.AUTOMOD_REQUEST_NEED_REASON)
        cleaned_rule, cleaned_proposal, cleaned_reason = parsed
        async with session_scope(self._bot.sessions) as session:
            row = AutomodRequest(
                guild_id=int(guild_id),
                author_id=int(author_id),
                rule=cleaned_rule,
                proposal=cleaned_proposal,
                reason=cleaned_reason,
                status=RequestStatus.NEW.value,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            await session.flush()
            request_id = int(row.id)
        view = request_card(
            request_id=request_id,
            author_id=author_id,
            rule=cleaned_rule,
            proposal=cleaned_proposal,
            reason=cleaned_reason,
            status=RequestStatus.NEW.value,
        )
        channel_copy = request_card(
            request_id=request_id,
            author_id=author_id,
            rule=cleaned_rule,
            proposal=cleaned_proposal,
            reason=cleaned_reason,
            status=RequestStatus.NEW.value,
            buttons=False,
        )
        await self._post_request_copy(guild_id, request_id, channel_copy)
        await self._dm_owners(view)
        return panel(copy.AUTOMOD_REQUEST_TITLE, copy.AUTOMOD_REQUEST_SENT, tone=Tone.OK)

    async def decide_request(self, request_id: int, *, accepted: bool, actor_id: int) -> Any:
        probe = getattr(self._bot, "is_developer", None)
        is_owner = bool(probe(int(actor_id))) if callable(probe) else False
        if not may_decide_automod(is_owner=is_owner):
            return denied(copy.AUTOMOD_OWNER_ONLY)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(AutomodRequest, int(request_id))
            if row is None:
                return denied(copy.STALE_BODY)
            if row.status != RequestStatus.NEW.value:
                return panel(copy.AUTOMOD_REQUEST_TITLE, copy.AUTOMOD_REQUEST_DECIDED, tone=Tone.MUTED)
            row.status = RequestStatus.ACCEPTED.value if accepted else RequestStatus.REJECTED.value
            row.decided_by = int(actor_id)
            row.decided_at = datetime.now(UTC)
            snapshot = (
                int(row.id),
                int(row.guild_id),
                int(row.author_id),
                str(row.rule),
                str(row.proposal),
                str(row.reason),
                str(row.status),
                None if row.channel_id is None else int(row.channel_id),
                None if row.message_id is None else int(row.message_id),
            )
        request_id, guild_id, author_id, rule, proposal, reason, status, channel_id, message_id = snapshot
        view = request_card(
            request_id=request_id,
            author_id=author_id,
            rule=rule,
            proposal=proposal,
            reason=reason,
            status=status,
            buttons=False,
        )
        if channel_id and message_id:
            guild = self._guild(guild_id)
            if guild is not None:
                await self._edit_message(guild, channel_id, message_id, view)
        notice = copy.AUTOMOD_AUTHOR_ACCEPTED if accepted else copy.AUTOMOD_AUTHOR_REJECTED
        await self._dm_user(author_id, panel(copy.AUTOMOD_REQUEST_TITLE, notice, tone=Tone.INFO))
        return panel(copy.AUTOMOD_REQUEST_TITLE, copy.automod_status_label(status), tone=Tone.OK)

    async def _rules_view(self, guild_id: int, *, ref: int) -> tuple[Any, str]:
        rules = getattr(self._bot, "automod_rules", None)
        config = getattr(rules, "config", None)
        if config is None:
            config = default_config()
        specs = rule_specs(config)
        lines = [copy.automod_rule_sentence(*item) for item in specs]
        exempt = copy.automod_exempt_line(
            config.exempt.ranks,
            administrators=config.exempt.administrators,
            roles=config.exempt.roles,
            channels=config.exempt.channels,
        )
        counts = await self._hits(guild_id)
        hits = copy.automod_hits_line(counts)
        path = "data/automod.yml"
        if rules is not None:
            raw = getattr(rules, "path", None)
            if raw is not None:
                path = f"data/{getattr(raw, 'name', 'automod.yml')}"
        changed = _from_mtime(None if rules is None else getattr(rules, "mtime", None))
        loaded = None if rules is None else getattr(rules, "loaded_at", None)
        digest = card_fingerprint(
            lines=lines,
            exempt=exempt,
            hits=tuple((name, counts.get(name, 0)) for name in AUTOMOD_RULES),
            mtime=None if changed is None else int(changed.timestamp()),
            loaded_at=None if loaded is None else int(loaded.timestamp()),
        )
        return (
            rules_card(
                lines=lines,
                exempt=exempt,
                hits=hits,
                file_path=path,
                changed_at=changed,
                loaded_at=loaded,
                ref=ref,
            ),
            digest,
        )

    async def _hits(self, guild_id: int) -> dict[str, int]:
        since = datetime.now(UTC) - timedelta(days=7)
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.execute(
                    select(AutomodHit.rule, func.count())
                    .where(AutomodHit.guild_id == int(guild_id), AutomodHit.created_at >= since)
                    .group_by(AutomodHit.rule)
                )
            ).all()
        found = {name: 0 for name in AUTOMOD_RULES}
        for name, total in rows:
            found[str(name)] = int(total)
        return found

    async def _post_request_copy(self, guild_id: int, request_id: int, view: Any) -> int | None:
        config = getattr(self._bot, "config_by_guild", {}).get(int(guild_id))
        channel_id = None if config is None else getattr(config, "automod_channel_id", None)
        guild = self._guild(int(guild_id))
        channel = self._channel(guild, int(channel_id)) if channel_id else None
        send = getattr(channel, "send", None)
        if not callable(send):
            return None
        try:
            message = await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("automod_request_copy_failed", guild_id=guild_id, error=str(exc))
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(AutomodRequest, int(request_id))
            if row is not None:
                row.channel_id = int(channel_id)
                row.message_id = int(message.id)
        return int(message.id)

    async def _dm_owners(self, view: Any) -> None:
        for user_id in _owner_ids(self._bot):
            await self._dm_user(user_id, view)

    async def _dm_user(self, user_id: int, view: Any) -> bool:
        user = None
        getter = getattr(self._bot, "get_user", None)
        if callable(getter):
            user = getter(int(user_id))
        if user is None:
            fetch = getattr(self._bot, "fetch_user", None)
            if callable(fetch):
                try:
                    user = await self._discord(fetch(int(user_id)))
                except discord.HTTPException:
                    return False
        send = getattr(user, "send", None)
        if not callable(send):
            return False
        try:
            await self._discord(send_card(user, view))
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    async def _edit_message(self, guild: object, channel_id: int, message_id: int, view: Any) -> bool:
        channel = self._channel(guild, int(channel_id))
        if channel is None:
            return False
        fetch = getattr(channel, "fetch_message", None)
        target = None
        if callable(fetch):
            try:
                target = await self._discord(fetch(int(message_id)))
            except discord.NotFound:
                return False
            except discord.HTTPException:
                return False
        if target is None:
            getter = getattr(channel, "get_partial_message", None)
            target = getter(int(message_id)) if callable(getter) else None
        edit = getattr(target, "edit", None)
        if not callable(edit):
            return False
        try:
            await self._discord(edit_card(target, view))
        except discord.NotFound:
            return False
        except discord.HTTPException as exc:
            log.warning("automod_card_edit_failed", channel_id=channel_id, error=str(exc))
            return False
        return True


def _from_mtime(mtime: float | None) -> datetime | None:
    if mtime is None:
        return None
    return datetime.fromtimestamp(float(mtime), tz=UTC)


def _owner_ids(bot: Any) -> set[int]:
    found: set[int] = set()
    settings = getattr(bot, "settings", None)
    for user_id in getattr(settings, "owner_user_ids", ()) or ():
        found.add(int(user_id))
    owner_id = getattr(bot, "owner_id", None)
    if owner_id:
        found.add(int(owner_id))
    for user_id in getattr(bot, "owner_ids", None) or ():
        found.add(int(user_id))
    return found


def _timeout_active(member: object | None, now: datetime) -> bool:
    if member is None:
        return False
    until = getattr(member, "timed_out_until", None)
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > now
