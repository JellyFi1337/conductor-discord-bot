"""Страж нажатий: пять шагов UI-KIT §8 в объявленном порядке, плюс лимитер из 1c."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from functools import wraps
from typing import TypeVar

import discord
import structlog

from conductor.domain.access import AccessDecision, AccessSnapshot, Rank, can_punish, decide, empty_rank_roles
from conductor.domain.mutations import MutationScope
from conductor.domain.staff_feed import ACTION_GUARD_FORGED
from conductor.presentation import copy
from conductor.presentation.layout import denied, respond, stale, wait
from conductor.presentation.persistent import format_custom_id, parse_custom_id

log = structlog.get_logger("conductor.guard")

T = TypeVar("T")
ResourceLoader = Callable[..., Awaitable["GuardResource | None"]]


class GuardStep(IntEnum):
    GUILD = 1
    RESOURCE = 2
    ACCESS = 3
    HIERARCHY = 4


@dataclass(frozen=True, slots=True)
class GuardResource:
    ref: str
    guild_id: int
    owner_id: int | None = None
    closed: bool = False
    target_id: int | None = None
    target_rank: Rank | None = None
    channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    ok: bool
    steps: tuple[GuardStep, ...]
    reason: str | None = None
    forged: bool = False
    resource: GuardResource | None = None
    rank: Rank = Rank.NONE


async def run_guard(
    interaction: discord.Interaction,
    *,
    ref: str,
    load: ResourceLoader,
    key: str | None = None,
    scope: MutationScope | None = None,
    hierarchy: bool = False,
    public: bool = False,
    send: bool = True,
    allow_dm: bool = False,
) -> GuardVerdict:
    """Проверки строго 1→5. Лимитер из 1c — после гильдии, до ресурса.

    `allow_dm=True` — единственное исключение шага 1: в ЛС гильдия может быть
    None, вместо неё проверяется владелец ресурса по `ref`.
    """
    seen: list[GuardStep] = []

    async def finish(
        view: discord.ui.LayoutView | None,
        *,
        reason: str,
        forged: bool = False,
        resource: GuardResource | None = None,
        rank: Rank = Rank.NONE,
    ) -> GuardVerdict:
        if send and view is not None:
            await respond(interaction, view)
        return GuardVerdict(False, tuple(seen), reason=reason, forged=forged, resource=resource, rank=rank)

    seen.append(GuardStep.GUILD)
    guild = interaction.guild
    dm_mode = bool(allow_dm) and guild is None
    if guild is None and not dm_mode:
        return await finish(denied(copy.PANEL_GUILD_ONLY), reason="guild")

    user = interaction.user
    probe = getattr(interaction.client, "is_developer", None)
    is_dev = bool(probe(user.id)) if callable(probe) else False

    limiter = getattr(interaction.client, "interactions", None)
    allow = getattr(limiter, "allow", None)
    allowed = True
    if guild is not None and not is_dev and callable(allow):
        try:
            allowed = bool(allow(guild.id, user.id))
        except TypeError:
            allowed = bool(allow(guild.id))
    if not allowed:
        deny_view = getattr(limiter, "deny_view", None)
        view = deny_view() if callable(deny_view) else wait()
        return await finish(view, reason="wait")

    seen.append(GuardStep.RESOURCE)
    resource = await load(guild_id=0 if dm_mode else int(guild.id), ref=str(ref))
    if resource is None or resource.closed:
        return await finish(stale(), reason="stale")

    seen.append(GuardStep.ACCESS)
    if dm_mode:
        if resource.owner_id is None or resource.owner_id != user.id:
            return await finish(denied(copy.TICKET_STRANGER), reason="denied", resource=resource)
        return GuardVerdict(True, tuple(seen), resource=resource)
    role_ids = tuple(role.id for role in getattr(user, "roles", ()))
    is_admin = bool(getattr(getattr(user, "guild_permissions", None), "administrator", False))
    decision = await _access_decision(
        interaction.client,
        guild_id=guild.id,
        user_id=user.id,
        member_role_ids=role_ids,
        is_administrator=is_admin,
        key=key,
    )
    owner = resource.owner_id is not None and resource.owner_id == user.id
    allowed = True if public or is_dev or decision.developer else owner or (key is not None and decision.allowed)
    forged = resource.guild_id != guild.id
    if forged or not allowed:
        if forged:
            await _report_forged(interaction, ref=str(ref), rank=decision.rank)
        need = copy.rank_label(decision.need.key) if decision.need is not None else None
        if forged:
            body = copy.FORGED_REF_DENIED
        elif decision.deny_copy:
            body = decision.deny_copy
            need = None
        elif decision.need is not None:
            body = copy.denied_actors(decision.need.key)
        else:
            body = copy.CONFIG_DENIED
        return await finish(
            denied(body, need=need),
            reason="forged" if forged else "denied",
            forged=forged,
            resource=resource,
            rank=decision.rank,
        )

    if hierarchy and not is_dev and not decision.developer:
        seen.append(GuardStep.HIERARCHY)
        target_rank = resource.target_rank
        if target_rank is None or not can_punish(decision.rank, target_rank):
            return await finish(
                denied(copy.HIERARCHY_DENIED),
                reason="hierarchy",
                resource=resource,
                rank=decision.rank,
            )

    return GuardVerdict(True, tuple(seen), resource=resource, rank=decision.rank)


def guarded(
    *,
    key: str | None = None,
    scope: MutationScope | None = None,
    hierarchy: bool = False,
    load: ResourceLoader | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T | None]]]:
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T | None]]:
        @wraps(fn)
        async def wrapper(self: object, interaction: discord.Interaction, *args: object, **kwargs: object) -> T | None:
            loader = load or getattr(self, "load_resource", None)
            if not callable(loader):
                raise TypeError("guarded: нужен load= или метод load_resource")
            item_key = key if key is not None else getattr(type(self), "access_key", None)
            item_scope = scope if scope is not None else getattr(type(self), "mutation_scope", None)
            item_hierarchy = hierarchy or bool(getattr(type(self), "checks_hierarchy", False))
            item_public = bool(getattr(type(self), "public", False))
            verdict = await run_guard(
                interaction,
                ref=str(getattr(self, "ref", "")),
                load=loader,
                key=item_key,
                scope=item_scope,
                hierarchy=item_hierarchy,
                public=item_public,
            )
            if not verdict.ok:
                return None
            return await fn(self, interaction, verdict, *args, **kwargs)

        return wrapper

    return decorator


async def _access_decision(
    client: object,
    *,
    guild_id: int,
    user_id: int,
    member_role_ids: tuple[int, ...],
    is_administrator: bool,
    key: str | None,
) -> AccessDecision:
    lookup_key = key or "remind.use"
    method = getattr(client, "decide_access", None)
    if callable(method):
        return await method(
            guild_id=guild_id,
            user_id=user_id,
            member_role_ids=member_role_ids,
            is_administrator=is_administrator,
            key=lookup_key,
        )
    snap = AccessSnapshot(empty_rank_roles(), {}, True)
    return decide(
        snap,
        key=lookup_key,
        member_role_ids=member_role_ids,
        is_administrator=is_administrator,
    )


async def _report_forged(interaction: discord.Interaction, *, ref: str, rank: Rank = Rank.NONE) -> None:
    data = getattr(interaction, "data", None)
    raw_id = data.get("custom_id") if isinstance(data, dict) else None
    parsed = parse_custom_id(str(raw_id)) if raw_id else None
    token = str(parsed) if parsed is not None else format_custom_id("xx", "ref", ref)
    guild = interaction.guild
    log.warning(
        "forged_ref",
        guild_id=None if guild is None else guild.id,
        user_id=interaction.user.id,
        ref=ref,
        custom_id=token,
        label="подделка ref",
    )
    bot = interaction.client
    configs = getattr(bot, "config_by_guild", None)
    config = configs.get(guild.id) if configs is not None and guild is not None else None
    channel_id = getattr(config, "admin_log_channel_id", None)
    submit = getattr(getattr(bot, "log_pump", None), "submit", None)
    if callable(submit) and channel_id:
        submit(channel_id, view=denied(copy.forged_ref_log(token)))
    post = getattr(getattr(bot, "staff_feed", None), "post", None)
    if callable(post) and guild is not None:
        await post(
            guild.id,
            actor_id=interaction.user.id,
            actor_rank=rank,
            action=ACTION_GUARD_FORGED,
            ref=ref,
            details=token,
        )
