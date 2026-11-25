"""Карточки /owner: wipe, массовые роли, импорт Old, каркас теста. Без sqlalchemy и без services."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress

import discord
from discord import app_commands, ui

from conductor.domain.create_lock import CreateHold
from conductor.domain.loyalty import TIER_LABELS, LoyaltyTier
from conductor.domain.ops import MassMode, WipeSection
from conductor.domain.seed import (
    CHANNEL_PAGES,
    ROLE_GROUPS,
    ROLE_PAGES,
    ExistingItem,
    SeedMode,
    SeedPlan,
    SeedReport,
    SeedVerdict,
    SkeletonEntry,
    blueprint_status,
    colour_hex,
    entries_in_category,
    entry_by_key,
    needs_connect,
    rights_key,
    skeleton_entries,
)
from conductor.domain.traffic import INVALID_BUDGET, GovernorSnapshot, TrafficSnapshot
from conductor.presentation import copy
from conductor.presentation.interact import ack, acked, instant
from conductor.presentation.layout import denied, error_code, panel, respond, update_message
from conductor.presentation.motion import InteractionTarget, Progress, StageState
from conductor.presentation.tokens import Icon, Tone

OwnerClick = Callable[[discord.Interaction], Awaitable[None]]
SameOwner = Callable[[discord.Interaction, int], bool]


async def owner_click(click: discord.Interaction, same_owner: SameOwner, actor_id: int) -> bool:
    if same_owner(click, actor_id):
        return True
    await respond(click, denied(copy.OWNER_DENIED, need=copy.OWNER_NEED))
    return False


def dismiss_owner(same_owner: SameOwner, actor_id: int, title: str) -> OwnerClick:
    async def no(click: discord.Interaction) -> None:
        if not await owner_click(click, same_owner, actor_id):
            return
        await update_message(click, ops_cancelled(title))

    return no

__all__ = [
    "GRANT_DESCRIBE",
    "MATCH_CHOICES",
    "PROTECT_CHOICES",
    "SEED_MODE_CHOICES",
    "STRIP_DESCRIBE",
    "TIER_CHOICES",
    "WIPE_CHOICES",
    "deploy_done",
    "dismiss_owner",
    "grant_confirm",
    "grant_done",
    "lift_done",
    "limits_confirm",
    "limits_done",
    "mass_done",
    "old_import_confirm",
    "old_import_done",
    "old_import_view",
    "ops_cancelled",
    "owner_click",
    "rep_done",
    "run_owner_limits",
    "run_owner_progress",
    "run_owner_seed",
    "run_seed_progress",
    "seed_blueprint",
    "show_emoji_deploy",
    "seed_confirm",
    "seed_done",
    "seed_preview",
    "strip_confirm",
    "strip_done",
    "wipe_confirm",
    "wipe_done",
]

STRIP_DESCRIBE = {
    "role": copy.CMD_OWNER_ROLE,
    "role2": copy.CMD_OWNER_ROLE2,
    "role3": copy.CMD_OWNER_ROLE3,
    "all_roles": copy.CMD_OWNER_ALL_ROLES,
    "have": copy.CMD_OWNER_HAVE,
    "have2": copy.CMD_OWNER_HAVE2,
    "have3": copy.CMD_OWNER_HAVE3,
    "have_mode": copy.CMD_OWNER_HAVE_MODE,
    "without": copy.CMD_OWNER_WITHOUT,
    "without2": copy.CMD_OWNER_WITHOUT2,
    "without3": copy.CMD_OWNER_WITHOUT3,
    "without_mode": copy.CMD_OWNER_WITHOUT_MODE,
    "protect": copy.CMD_OWNER_PROTECT,
    "skip_timeout": copy.CMD_OWNER_SKIP_TIMEOUT,
    "skip_pending": copy.CMD_OWNER_SKIP_PENDING,
    "skip_boost": copy.CMD_OWNER_SKIP_BOOST,
}
GRANT_DESCRIBE = {key: value for key, value in STRIP_DESCRIBE.items() if key != "all_roles"}
WIPE_CHOICES = [
    app_commands.Choice(name=copy.OWNER_WIPE_CHOICES[section.value], value=section.value) for section in WipeSection
]
SEED_MODE_CHOICES = [
    app_commands.Choice(name=copy.CMD_OWNER_SEED_MODE_BUILD, value=SeedMode.BUILD.value),
    app_commands.Choice(name=copy.CMD_OWNER_SEED_MODE_ADOPT, value=SeedMode.ADOPT.value),
]
TIER_CHOICES = [
    app_commands.Choice(name="Ruby Old", value=LoyaltyTier.RUBY.value),
    app_commands.Choice(name="Emerald Old", value=LoyaltyTier.EMERALD.value),
    app_commands.Choice(name="Sapphire Old", value=LoyaltyTier.SAPPHIRE.value),
    app_commands.Choice(name="Amethyst Old", value=LoyaltyTier.AMETHYST.value),
]
MATCH_CHOICES = [
    app_commands.Choice(name=copy.OWNER_MATCH_ANY, value="any"),
    app_commands.Choice(name=copy.OWNER_MATCH_ALL, value="all"),
]
PROTECT_CHOICES = [
    app_commands.Choice(name=copy.OWNER_PROTECT_NONE, value="none"),
    app_commands.Choice(name=copy.OWNER_PROTECT_STAFF, value="staff"),
    app_commands.Choice(name=copy.OWNER_PROTECT_OLD, value="old"),
    app_commands.Choice(name=copy.OWNER_PROTECT_STAFF_OLD, value="staff_old"),
]


def wipe_confirm(
    *,
    section: WipeSection,
    on_yes: OwnerClick,
    on_no: OwnerClick,
) -> ui.LayoutView:
    yes = ui.Button(label=copy.wipe_verb(section), style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.OWNER_WIPE_CONFIRM,
        copy.wipe_hint(section),
        tone=Tone.DANGER,
        icon=Icon.WARN,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
    )


def wipe_done(*, section: WipeSection, rows: int) -> ui.LayoutView:
    tone = Tone.MUTED if rows <= 0 else Tone.OK
    return panel(copy.OWNER_WIPE_TITLE, copy.wipe_done_body(section=section, rows=rows), tone=tone)


def lift_done(*, days: int, score: int, step: str, verified: bool) -> ui.LayoutView:
    return panel(
        copy.OWNER_LIFT_TITLE,
        copy.owner_lift_body(days=days, score=score, step=step, verified=verified),
        tone=Tone.OK,
    )


def rep_done(*, before: int, after: int) -> ui.LayoutView:
    return panel(copy.OWNER_REP_TITLE, copy.owner_rep_body(before=before, after=after), tone=Tone.OK)


def deploy_done(report: object) -> ui.LayoutView:
    retry = getattr(report, "retry_after", None)
    if retry:
        body = copy.seed_blocked("emoji", retry)
        created = int(getattr(report, "created", 0))
        if created:
            body = f"{body}\n{copy.deploy_created_before_limit(created)}"
        return panel(copy.DEPLOY_TITLE, body, tone=Tone.DANGER)
    loaded = int(getattr(report, "loaded", 0))
    failed = int(getattr(report, "failed", 0))
    body = copy.deploy_body(
        created=int(getattr(report, "created", 0)),
        skipped=int(getattr(report, "skipped", 0)),
        loaded=loaded,
        total=int(getattr(report, "total", 0)),
        failed=failed,
    )
    return panel(copy.DEPLOY_TITLE, body, tone=Tone.OK if loaded and not failed else Tone.WARN)


def strip_confirm(
    *,
    mode: MassMode,
    roles: str,
    members: int,
    on_yes: OwnerClick,
    on_no: OwnerClick,
    filters: str = "",
) -> ui.LayoutView:
    yes = ui.Button(label=copy.OWNER_STRIP_VERB, style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.OWNER_STRIP_CONFIRM,
        copy.strip_hint(mode=mode, roles=roles, members=members, filters=filters),
        tone=Tone.DANGER,
        icon=Icon.WARN,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
    )


def strip_done(
    *,
    changed: int,
    failed: int,
    skipped: int,
    registry: int,
    skipped_roles: str = "",
    skipped_require: int = 0,
    skipped_except: int = 0,
    skipped_protect: int = 0,
    skipped_timeout: int = 0,
    skipped_pending: int = 0,
    skipped_boost: int = 0,
) -> ui.LayoutView:
    tone = Tone.WARN if failed or skipped_roles else Tone.OK
    return panel(
        copy.OWNER_STRIP_TITLE,
        copy.roles_done_body(
            changed=changed,
            failed=failed,
            skipped=skipped,
            registry=registry,
            grant=False,
            skipped_roles=skipped_roles,
            skipped_require=skipped_require,
            skipped_except=skipped_except,
            skipped_protect=skipped_protect,
            skipped_timeout=skipped_timeout,
            skipped_pending=skipped_pending,
            skipped_boost=skipped_boost,
        ),
        tone=tone,
    )


def grant_confirm(
    *,
    roles: str,
    members: int,
    on_yes: OwnerClick,
    on_no: OwnerClick,
    filters: str = "",
) -> ui.LayoutView:
    yes = ui.Button(label=copy.OWNER_GRANT_VERB, style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.OWNER_GRANT_CONFIRM,
        copy.grant_hint(roles=roles, members=members, filters=filters),
        tone=Tone.DANGER,
        icon=Icon.WARN,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
    )


def grant_done(
    *,
    changed: int,
    failed: int,
    skipped: int,
    registry: int,
    skipped_roles: str = "",
    skipped_require: int = 0,
    skipped_except: int = 0,
    skipped_protect: int = 0,
    skipped_timeout: int = 0,
    skipped_pending: int = 0,
    skipped_boost: int = 0,
) -> ui.LayoutView:
    tone = Tone.WARN if failed or skipped_roles else Tone.OK
    return panel(
        copy.OWNER_GRANT_TITLE,
        copy.roles_done_body(
            changed=changed,
            failed=failed,
            skipped=skipped,
            registry=registry,
            grant=True,
            skipped_roles=skipped_roles,
            skipped_require=skipped_require,
            skipped_except=skipped_except,
            skipped_protect=skipped_protect,
            skipped_timeout=skipped_timeout,
            skipped_pending=skipped_pending,
            skipped_boost=skipped_boost,
        ),
        tone=tone,
    )


def old_import_confirm(
    *,
    role: str,
    tier: LoyaltyTier,
    members: int,
    on_yes: OwnerClick,
    on_no: OwnerClick,
) -> ui.LayoutView:
    yes = ui.Button(label=copy.OWNER_OLD_VERB, style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.OWNER_OLD_CONFIRM,
        copy.old_import_hint(role=role, tier=TIER_LABELS[tier], members=members),
        tone=Tone.DANGER,
        icon=Icon.WARN,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
    )


def old_import_done(
    *,
    added: int,
    updated: int,
    same: int,
    bots: int,
    enforced: int,
    failed: int,
    unbound: bool,
) -> ui.LayoutView:
    tone = Tone.WARN if failed or unbound else Tone.OK
    return panel(
        copy.OWNER_OLD_TITLE,
        copy.old_import_done_body(
            added=added,
            updated=updated,
            same=same,
            bots=bots,
            enforced=enforced,
            failed=failed,
            unbound=unbound,
        ),
        tone=tone,
    )


def ops_cancelled(title: str) -> ui.LayoutView:
    return panel(title, copy.OWNER_CANCELLED, tone=Tone.MUTED)


def limits_confirm(*, body: str, on_yes: OwnerClick, on_no: OwnerClick) -> ui.LayoutView:
    yes = ui.Button(label=copy.OWNER_LIMITS_CLEAR, style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.OWNER_LIMITS_TITLE,
        body,
        tone=Tone.WARN,
        icon=Icon.WARN,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
    )


def limits_done() -> ui.LayoutView:
    return panel(copy.OWNER_LIMITS_TITLE, copy.OWNER_LIMITS_DONE, tone=Tone.OK)


def limits_body(
    packed: Sequence[tuple[object, bool]],
    *,
    now: float,
    traffic: object | None = None,
    governor: object | None = None,
) -> str:
    chunks: list[str] = []
    if isinstance(traffic, TrafficSnapshot):
        chunks.extend(
            (
                copy.traffic_rps(traffic.rps_1s, traffic.rps_60s),
                copy.traffic_invalid(traffic.invalid_10m, INVALID_BUDGET),
                copy.traffic_global_429(traffic.global_429_10m),
                copy.traffic_top("429", traffic.top_429),
                copy.traffic_top("403", traffic.top_403),
            )
        )
        if traffic.buckets:
            chunks.append(copy.OWNER_LIMITS_BUSY)
            for item in traffic.buckets[:10]:
                chunks.append(
                    copy.traffic_bucket(item.template, item.remaining, item.limit, item.reset_after)
                )
    if isinstance(governor, GovernorSnapshot):
        chunks.extend(
            (
                copy.traffic_profile(governor.profile),
                copy.traffic_rps_goal(
                    0 if not isinstance(traffic, TrafficSnapshot) else traffic.rps_1s,
                    governor.rps_target,
                ),
                copy.traffic_fuse(governor.fuse),
            )
        )
        for item in governor.lanes:
            chunks.append(copy.traffic_lane_wait(item.lane, item.queue, item.p50, item.p95))
        for item in governor.allowances:
            chunks.append(copy.traffic_aimd(item.bucket, item.allowance, frozen=item.frozen))
    if not packed:
        chunks.append(copy.OWNER_LIMITS_EMPTY)
        chunks.append(copy.OWNER_LIMITS_WARN)
        return "\n".join(chunks)
    chunks.extend(
        copy.diagnose_create_hold(
            hold.bucket,
            str(hold.source),
            hold.remaining(now),
            persisted=persisted,
        )
        for hold, persisted in packed
    )
    chunks.append(copy.OWNER_LIMITS_WARN)
    return "\n".join(chunks)


async def run_owner_limits(
    interaction: discord.Interaction,
    *,
    packed: Sequence[tuple[object, bool]],
    now: float,
    same_owner: SameOwner,
    actor_id: int,
    on_clear: Callable[[], None],
    observer: object | None = None,
) -> None:
    traffic = None
    snapper = getattr(observer, "snapshot", None)
    if callable(snapper):
        traffic = snapper()
    gov = None
    viewer = getattr(getattr(getattr(interaction, "client", None), "traffic", None), "view", None)
    if callable(viewer):
        guild = getattr(interaction, "guild", None)
        gov = viewer(0 if guild is None else int(getattr(guild, "id", 0) or 0))

    async def yes(click: discord.Interaction) -> None:
        if not await owner_click(click, same_owner, actor_id):
            return
        if not await ack(click):
            return
        on_clear()
        await InteractionTarget(click).edit(limits_done())

    cancel = dismiss_owner(same_owner, actor_id, copy.OWNER_LIMITS_TITLE)
    await respond(
        interaction,
        limits_confirm(body=limits_body(packed, now=now, traffic=traffic, governor=gov), on_yes=yes, on_no=cancel),
    )


async def run_owner_seed(
    interaction: discord.Interaction,
    *,
    guild: discord.Guild,
    seed: object,
    motion: object,
    with_old: bool,
    mode: str | None,
    actor_id: int,
    on_map: OwnerClick | None,
) -> None:
    preview, blocked = await seed.command_gate(guild, with_old=with_old)  # type: ignore[attr-defined]
    if blocked is not None:
        await respond(interaction, denied(blocked, need=copy.OWNER_NEED))
        return
    assert preview is not None
    ready = bool(seed.bot_can_seed(guild))  # type: ignore[attr-defined]
    seed_mode = SeedMode.BUILD if mode is None else SeedMode(mode)

    async def card(report: SeedReport | None = None) -> object:
        fresh, existing = await seed.catalog(guild, with_old=with_old)  # type: ignore[attr-defined]
        return seed_blueprint(
            fresh,
            existing=existing,
            with_old=with_old,
            no_perms=not ready,
            report=report,
            on_map=on_map,
        )

    if ready and (needs_connect(preview) or preview.create):
        await run_seed_progress(
            interaction,
            registry=motion,
            run=lambda progress: seed.run(  # type: ignore[attr-defined]
                guild, actor_id=actor_id, with_old=with_old, mode=seed_mode, on_progress=progress
            ),
            done=card,
        )
        return
    await respond(interaction, await card())


async def show_emoji_deploy(
    interaction: discord.Interaction,
    *,
    report: object,
    attach: Callable[[], None],
    now: float,
) -> None:
    if isinstance(report, CreateHold):
        await respond(
            interaction,
            denied(copy.seed_blocked("emoji", report.remaining(now), source=str(report.source))),
        )
        return
    attach()
    await respond(interaction, deploy_done(report))


def seed_confirm(*, on_yes: OwnerClick, on_no: OwnerClick) -> ui.LayoutView:
    del on_no
    return seed_blueprint(SeedPlan(create=(), reuse=(), repair=()), existing={}, on_bind=on_yes)


def seed_preview(plan: SeedPlan, *, on_yes: OwnerClick, on_no: OwnerClick) -> ui.LayoutView:
    del on_no
    existing = {key: ExistingItem() for key in (*plan.create, *plan.reuse, *plan.repair, *plan.by_name)}
    return seed_blueprint(plan, existing=existing, can_bind=True, on_bind=on_yes)


def seed_blueprint(
    plan: SeedPlan,
    *,
    existing: Mapping[str, ExistingItem],
    with_old: bool = False,
    page: int = 1,
    can_bind: bool = False,
    no_perms: bool = False,
    on_bind: OwnerClick | None = None,
    on_map: OwnerClick | None = None,
    report: SeedReport | None = None,
) -> ui.LayoutView:
    pages = _blueprint_pages(existing, with_old=with_old, report=report)
    total = max(1, len(pages))
    current = max(1, min(int(page), total))
    if no_perms:
        footnote = copy.OWNER_SEED_NO_PERMS
    elif can_bind:
        footnote = copy.SEED_BIND_HINT
    else:
        footnote = copy.SEED_EMPTY_HINT
    body = f"{pages[current - 1]}\n\n{footnote}\n{copy.OWNER_SEED_HINT}"
    rows: list[ui.ActionRow] = []
    nav: list[ui.Button] = []
    if total > 1:
        prev = ui.Button(label="←", style=discord.ButtonStyle.secondary, disabled=current <= 1)
        nxt = ui.Button(label="→", style=discord.ButtonStyle.secondary, disabled=current >= total)

        async def go_prev(click: discord.Interaction) -> None:
            await update_message(click, seed_blueprint(
                    plan,
                    existing=existing,
                    with_old=with_old,
                    page=current - 1,
                    can_bind=can_bind,
                    no_perms=no_perms,
                    on_bind=on_bind,
                    on_map=on_map,
                    report=report,
                ))

        async def go_next(click: discord.Interaction) -> None:
            await update_message(click, seed_blueprint(
                    plan,
                    existing=existing,
                    with_old=with_old,
                    page=current + 1,
                    can_bind=can_bind,
                    no_perms=no_perms,
                    on_bind=on_bind,
                    on_map=on_map,
                    report=report,
                ))

        prev.callback = instant(go_prev)
        nxt.callback = instant(go_next)
        nav.extend((prev, nxt))
    if can_bind and on_bind is not None:
        bind = ui.Button(label=copy.OWNER_SEED_VERB, style=discord.ButtonStyle.success)
        bind.callback = instant(on_bind)
        nav.append(bind)
    if on_map is not None:
        mapped = ui.Button(label=copy.SETUP_MAP_OPEN, style=discord.ButtonStyle.secondary)
        mapped.callback = instant(on_map)
        nav.append(mapped)
    if nav:
        rows.append(ui.ActionRow(*nav))
    return panel(
        copy.OWNER_SEED_TITLE,
        body,
        tone=Tone.OK
        if not plan.create and not plan.repair and (report is None or report.verdict is SeedVerdict.OK)
        else Tone.WARN,
        icon=Icon.WARN,
        rows=tuple(rows),
        timeout=300.0,
    )


def _entry_mark(entry: SkeletonEntry, item: ExistingItem) -> str:
    return copy.seed_status_label(blueprint_status(entry, item).value)


def _channel_lines(entry: SkeletonEntry, item: ExistingItem) -> list[str]:
    mark = _entry_mark(entry, item)
    line = f"{entry.diagnose_label} · {copy.seed_rights_text(rights_key(entry))} · {mark}"
    bits: list[str] = []
    why = copy.seed_why(entry.key)
    if why:
        bits.append(why)
    if entry.category:
        parent = entry_by_key(entry.category)
        if parent is not None:
            bits.append(copy.seed_in_category(parent.name))
    if entry.aliases:
        bits.append(copy.seed_also_named(", ".join(entry.aliases)))
    if bits:
        return [line, f"  {' · '.join(bits)}"]
    return [line]


def _role_lines(entry: SkeletonEntry, item: ExistingItem) -> list[str]:
    mark = _entry_mark(entry, item)
    hoist = copy.SEED_HOIST_YES if entry.hoist else copy.SEED_HOIST_NO
    extra = f" · {copy.SEED_OPTIONAL}" if entry.optional else ""
    line = f"{colour_hex(entry.colour)} {entry.name} · {hoist} · {mark}{extra}"
    bits: list[str] = []
    why = copy.seed_why(entry.key)
    if why:
        bits.append(why)
    if entry.aliases:
        bits.append(copy.seed_also_named(", ".join(entry.aliases)))
    if bits:
        return [line, f"  {' · '.join(bits)}"]
    return [line]


def _blueprint_pages(
    existing: Mapping[str, ExistingItem],
    *,
    with_old: bool,
    report: SeedReport | None = None,
) -> list[str]:
    counts = {"missing": 0, "found": 0, "bound": 0, "repair": 0}
    for entry in skeleton_entries(with_old=with_old):
        status = blueprint_status(entry, existing.get(entry.key, ExistingItem())).value
        counts[status] = counts.get(status, 0) + 1
    header = copy.seed_blueprint_summary(
        missing=counts["missing"],
        found=counts["found"],
        bound=counts["bound"],
        repair=counts["repair"],
    )
    if report is not None:
        header = f"{header}\n{copy.seed_connect_line(by_name=len(report.by_name), repaired=report.repaired)}"
        header += "\n" + copy.seed_result_line(
            created=report.created, held=len(report.held), failed=len(report.failed)
        )
    # Причины видны сразу, но не раздувают каждую страницу каркаса.
    # Последнее примечание содержит итог остановки по бюджету времени.
    details = ""
    if report is not None and report.notes:
        notes = list(dict.fromkeys(str(note) for note in report.notes if note))
        if len(notes) > 3:
            notes = [*notes[:2], notes[-1]]
        details = "\n".join(note if len(note) <= 240 else note[:239] + "…" for note in notes)
    pages = [
        "\n".join(
            (
                header,
                details,
                "",
                copy.SEED_GROUP_HOW,
                copy.SEED_HOW_BODY,
                "",
                copy.SEED_GROUP_WHY,
                copy.SEED_WHY_BODY,
                "",
                copy.SEED_GROUP_LIMITS,
                copy.SEED_LIMITS_BODY,
            )
        )
    ]
    groups = dict(ROLE_GROUPS)
    for cat_keys in CHANNEL_PAGES:
        blocks = [header, ""]
        for cat_key in cat_keys:
            rows = entries_in_category(cat_key, with_old=with_old)
            if not rows:
                continue
            blocks.append(copy.seed_group_title(cat_key))
            hint = copy.seed_group_hint(cat_key)
            if hint:
                blocks.append(hint)
            for entry in rows:
                blocks.extend(_channel_lines(entry, existing.get(entry.key, ExistingItem())))
            blocks.append("")
        pages.append("\n".join(blocks).rstrip())
    for group_keys in ROLE_PAGES:
        if group_keys == ("old",) and not with_old:
            continue
        blocks = [header, ""]
        for group in group_keys:
            blocks.append(copy.seed_group_title(group))
            hint = copy.seed_group_hint(group)
            if hint:
                blocks.append(hint)
            for key in groups.get(group, ()):
                entry = entry_by_key(key)
                if entry is None or (entry.optional and not with_old):
                    continue
                blocks.extend(_role_lines(entry, existing.get(key, ExistingItem())))
            blocks.append("")
        pages.append("\n".join(blocks).rstrip())
    return pages


def mass_done(report: object, *, grant: bool) -> ui.LayoutView:
    skipped_roles = getattr(report, "skipped_roles", ())
    payload = {
        "changed": int(getattr(report, "changed", 0)),
        "failed": int(getattr(report, "failed", 0)),
        "skipped": int(getattr(report, "skipped", 0)),
        "registry": int(getattr(report, "registry", 0)),
        "skipped_roles": " ".join(skipped_roles),
        "skipped_require": int(getattr(report, "skipped_require", 0)),
        "skipped_except": int(getattr(report, "skipped_except", 0)),
        "skipped_protect": int(getattr(report, "skipped_protect", 0)),
        "skipped_timeout": int(getattr(report, "skipped_timeout", 0)),
        "skipped_pending": int(getattr(report, "skipped_pending", 0)),
        "skipped_boost": int(getattr(report, "skipped_boost", 0)),
    }
    return grant_done(**payload) if grant else strip_done(**payload)


def old_import_view(report: object) -> ui.LayoutView:
    plan = getattr(report, "plan", None)
    return old_import_done(
        added=int(getattr(report, "added", 0)),
        updated=int(getattr(report, "updated", 0)),
        same=int(getattr(plan, "skipped_same", 0)),
        bots=int(getattr(plan, "skipped_bot", 0)),
        enforced=int(getattr(report, "enforced", 0)),
        failed=int(getattr(report, "failed", 0)),
        unbound=bool(getattr(report, "unbound", False)),
    )


def seed_done(report: SeedReport) -> ui.LayoutView:
    if report.verdict is SeedVerdict.RATE_LIMITED:
        body = "\n".join(str(note) for note in report.notes if note) or copy.seed_blocked("role", 0)
        return panel(copy.OWNER_SEED_TITLE, body, tone=Tone.DANGER)
    if report.verdict is SeedVerdict.OK:
        tone = Tone.OK
    elif report.verdict in {SeedVerdict.PARTIAL, SeedVerdict.HELD}:
        tone = Tone.WARN
    else:
        tone = Tone.DANGER
    return panel(
        copy.OWNER_SEED_TITLE,
        copy.seed_done_body(
            created=report.created,
            reused=report.reused,
            repaired=report.repaired,
            created_roles=report.created_roles,
            reused_roles=report.reused_roles,
            created_channels=report.created_channels,
            reused_channels=report.reused_channels,
            created_categories=report.created_categories,
            bound=report.bound,
            notes=report.notes,
            special_granted=report.special_granted,
            humans=report.humans,
            by_name=report.by_name,
            missing=report.missing,
            held=report.held,
            failed=report.failed,
        ),
        tone=tone,
    )


async def run_owner_progress(
    click: discord.Interaction,
    *,
    title: str,
    registry: object,
    run: Callable[[Callable[[int, int], Awaitable[None]]], Awaitable[object]],
    done: Callable[[object], object],
    unit: str,
) -> None:
    if not await ack(click):
        return
    progress = Progress(
        InteractionTarget(click),
        title=title,
        registry=registry,  # type: ignore[arg-type]
        code=error_code(click),
        unit=unit,
    )

    async def on_progress(step: int, total: int) -> None:
        await progress.step(step, total=total)

    try:
        async with progress:
            await progress.start()
            report = await run(on_progress)
            await progress.finish(done(report))  # type: ignore[arg-type]
    except Exception as exc:
        await _report_click_error(click, exc)


async def run_seed_progress(
    click: discord.Interaction,
    *,
    registry: object,
    run: Callable[[Callable[[str, bool], Awaitable[None]]], Awaitable[SeedReport]],
    done: Callable[[SeedReport], object] | None = None,
) -> None:
    if not await ack(click):
        return
    progress = Progress(
        InteractionTarget(click),
        title=copy.OWNER_SEED_TITLE,
        footnote=copy.SEED_BUILD_WAIT,
        registry=registry,  # type: ignore[arg-type]
        code=error_code(click),
        show_after=0,
        min_interval=0,
    )

    async def on_progress(name: str, ok: bool) -> None:
        await progress.stage(name, StageState.DONE if ok else StageState.FAIL)

    try:
        async with progress:
            await progress.start()
            try:
                report = await run(on_progress)
            except Exception as exc:
                await _report_click_error(click, exc)
                return
            try:
                view = seed_done(report) if done is None else done(report)
                if inspect.isawaitable(view):
                    view = await view
            except Exception:
                view = seed_done(report)
            await progress.finish(view)
    except Exception as exc:
        await _report_click_error(click, exc)


async def _report_click_error(click: discord.Interaction, error: BaseException) -> None:
    reporter = getattr(click.client, "errors", None)
    report = getattr(reporter, "report_interaction", None)
    if callable(report):
        with suppress(Exception):
            await report(click, error, notify_user=False)
