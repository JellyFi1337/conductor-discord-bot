"""Страница /admin «Каркас»: обзор, каналы, роли, витрины, уведомления."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from conductor.domain.access import SKELETON_BIND, SKELETON_BUILD, SKELETON_MANAGE, SKELETON_READ
from conductor.domain.admin import (
    PAGE_SKELETON,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    ReadyState,
)
from conductor.domain.seed import (
    INGEST_DEBOUNCE,
    SeedMode,
    SeedPlan,
    SeedReport,
    SeedVerdict,
    SkeletonCheck,
    SkeletonKind,
    SkeletonStatus,
    entry_by_key,
    feature_verdicts,
    skeleton_entries,
)
from conductor.domain.showcase import SHOWCASES, ShowcaseState
from conductor.presentation import copy
from conductor.presentation.setup_wizard import (
    LEAF_CHANNELS,
    LEAF_NOTIFY,
    LEAF_OVERVIEW,
    LEAF_ROLES,
    LEAF_SHOWCASE,
    SKELETON_LEAVES,
    leaf_nav,
    plan_question,
)
from conductor.services.role_broker import RoleSubsystem

__all__ = ["PAGE", "commit_build", "execute_build"]

ProgressFn = Callable[[str, bool], Awaitable[None]]

_CURSOR: dict[tuple[int, int, str], int] = {}


def _leaf(value: int) -> int:
    return min(max(1, int(value)), SKELETON_LEAVES)


def _cursor(ctx: AdminCtx, kind: str) -> int:
    return _CURSOR.get((int(ctx.guild_id), int(ctx.actor_id), kind), 0)


def _set_cursor(ctx: AdminCtx, kind: str, index: int) -> None:
    _CURSOR[(int(ctx.guild_id), int(ctx.actor_id), kind)] = max(0, int(index))


def _channel_entries():
    return tuple(entry for entry in skeleton_entries() if entry.kind is not SkeletonKind.ROLE)


def _role_entries():
    return tuple(entry for entry in skeleton_entries() if entry.kind is SkeletonKind.ROLE)


def _seed(ctx: AdminCtx):
    return getattr(ctx.bot, "seed", None)


async def _checks(ctx: AdminCtx) -> tuple[SkeletonCheck, ...]:
    probe = getattr(_seed(ctx), "diagnose_checks", None)
    if not callable(probe) or ctx.guild is None:
        return ()
    return tuple(await probe(ctx.guild))


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report, ready_issue

    checks = await _checks(ctx)
    if not checks:
        return pack_report([ready_issue("empty", fix_control="skel.build")])
    issues = []
    ok = 0
    for verdict in feature_verdicts(checks):
        if verdict.status is SkeletonStatus.OK:
            ok += 1
            continue
        key = next(iter((*verdict.missing, *verdict.broken)), "")
        code = "unbound" if verdict.status is SkeletonStatus.FAIL else "member_perms"
        if verdict.note:
            code = "panel_stale"
        issues.append(ready_issue(code, fix_control="skel.build", skeleton_key=key))
    if not issues:
        return pack_report(())
    if ok:
        return pack_report(issues, state=ReadyState.PARTIAL)
    return pack_report(issues)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    current = _leaf(leaf)
    if current == LEAF_CHANNELS:
        return await _snap_channels(ctx, current)
    if current == LEAF_ROLES:
        return await _snap_roles(ctx, current)
    if current == LEAF_SHOWCASE:
        return await _snap_showcases(ctx, current)
    if current == LEAF_NOTIFY:
        return await _snap_notify(ctx, current)
    return await _snap_overview(ctx, current)


async def _snap_overview(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    checks = await _checks(ctx)
    verdicts = feature_verdicts(checks) if checks else ()
    fields: dict[str, object] = {}
    for verdict in verdicts:
        label = str(getattr(copy, verdict.label, verdict.feature))
        if verdict.status is SkeletonStatus.OK:
            fields[label] = copy.ADMIN_READY
        elif verdict.status is SkeletonStatus.FAIL:
            fields[label] = copy.ADMIN_NOT_READY
        else:
            fields[label] = copy.ADMIN_PARTIAL
    ok = sum(1 for item in verdicts if item.status is SkeletonStatus.OK)
    total = len(verdicts)
    if total and ok == total:
        fields[copy.DIAGNOSE_READY_OK] = copy.ADMIN_READY
    elif total:
        fields[copy.diagnose_ready_partial(ok=ok, total=total)] = copy.ADMIN_PARTIAL
    else:
        fields[copy.DIAGNOSE_READY_FAIL] = copy.ADMIN_NOT_READY
    controls = [
        *leaf_nav(leaf),
        PageControl("button", "skel.build", copy.SKELETON_BUILD),
        PageControl("button", "skel.map", copy.SETUP_MAP_OPEN),
        PageControl("button", "skel.rights", copy.DIAGNOSE_REAPPLY),
        PageControl("button", "skel.show", copy.SHOWCASE_POST_MISSING),
    ]
    return PageSnapshot(fields=fields, controls=tuple(controls), leaf=leaf)


async def _snap_channels(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    from conductor.services.admin_pages import channel_pick_control

    entries = _channel_entries()
    index = min(_cursor(ctx, "chan"), max(0, len(entries) - 1)) if entries else 0
    entry = entries[index] if entries else None
    fields: dict[str, object] = {}
    controls: list[PageControl] = [*leaf_nav(leaf)]
    if entry is not None:
        bound = await _bound_id(ctx, entry.key)
        fields[copy.skeleton_row_status(entry.diagnose_label, bound=bound is not None)] = (
            copy.listing_page(index + 1, len(entries))
        )
        parent_id = None if entry.category is None else await _bound_id(ctx, entry.category)
        types = _channel_types(entry.kind)
        if entry.kind is SkeletonKind.CATEGORY:
            controls.append(
                PageControl(
                    "channel",
                    f"skel.chan.bind:{entry.key}",
                    copy.SKELETON_PICK_OTHER,
                    channel_types=("category",),
                    current=None if bound is None else str(bound),
                )
            )
        else:
            controls.append(
                await channel_pick_control(
                    ctx,
                    f"skel.chan.bind:{entry.key}",
                    copy.SKELETON_PICK_OTHER,
                    parent_id=parent_id,
                    channel_types=types,
                    current=None if bound is None else str(bound),
                )
            )
        controls.append(PageControl("button", f"skel.chan.create:{entry.key}", copy.SKELETON_CREATE_ROW))
        if index > 0:
            controls.append(PageControl("button", "skel.chan.prev", copy.ADMIN_PREV))
        if index + 1 < len(entries):
            controls.append(PageControl("button", "skel.chan.next", copy.ADMIN_NEXT))
    return PageSnapshot(fields=fields, controls=tuple(controls), leaf=leaf)


async def _snap_roles(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    from conductor.services.admin_pages import role_pick_control

    entries = _role_entries()
    index = min(_cursor(ctx, "role"), max(0, len(entries) - 1)) if entries else 0
    entry = entries[index] if entries else None
    fields: dict[str, object] = {}
    controls: list[PageControl] = [*leaf_nav(leaf)]
    if entry is not None:
        bound = await _bound_id(ctx, entry.key)
        fields[copy.skeleton_row_status(entry.diagnose_label, bound=bound is not None)] = (
            copy.listing_page(index + 1, len(entries))
        )
        controls.append(
            await role_pick_control(
                ctx,
                f"skel.role.bind:{entry.key}",
                copy.SKELETON_PICK_OTHER,
                subsystem=_role_subsystem(entry.key),
                current=None if bound is None else str(bound),
                allow_create=False,
            )
        )
        controls.append(PageControl("button", f"skel.role.create:{entry.key}", copy.SKELETON_CREATE_ROW))
        if index > 0:
            controls.append(PageControl("button", "skel.role.prev", copy.ADMIN_PREV))
        if index + 1 < len(entries):
            controls.append(PageControl("button", "skel.role.next", copy.ADMIN_NEXT))
    return PageSnapshot(fields=fields, controls=tuple(controls), leaf=leaf)


async def _snap_showcases(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    from conductor.presentation.showcase import showcase_line

    service = getattr(ctx.bot, "showcase", None)
    guild = ctx.guild
    states = {} if service is None or guild is None else await service.statuses(guild)
    bound = {} if service is None else await service.bound_ids(ctx.guild_id)
    fields: dict[str, object] = {}
    for spec in SHOWCASES:
        state = states.get(spec.id, ShowcaseState.OFF)
        fields[spec.label] = showcase_line(state, bound.get(spec.channel_key))
    controls = [
        *leaf_nav(leaf),
        PageControl("button", "skel.show", copy.SHOWCASE_POST_MISSING),
    ]
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.SHOWCASE_HINT, leaf=leaf)


async def _snap_notify(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    bound = await _bound_id(ctx, "structure")
    on = bound is not None
    fields = {
        copy.SKELETON_NOTIFY_ON if on else copy.SKELETON_NOTIFY_OFF: (
            f"<#{bound}>" if bound is not None else "—"
        )
    }
    controls = [
        *leaf_nav(leaf),
        PageControl(
            "channel",
            "skel.notify.bind",
            copy.SKELETON_PICK_OTHER,
            channel_types=("text",),
            current=None if bound is None else str(bound),
        ),
        PageControl(
            "button",
            "skel.notify.toggle",
            copy.admin_toggle(on, copy.ADMIN_NOTIFY_SUBJECT),
        ),
    ]
    return PageSnapshot(fields=fields, controls=tuple(controls), leaf=leaf)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id.startswith("skel.leaf:"):
        return AdminResult(ok=True, leaf=_leaf(_suffix_int(control_id, LEAF_OVERVIEW)))
    if control_id == "skel.build":
        return await _confirm_build(ctx)
    if control_id == "skel.build.yes":
        return AdminResult(ok=True, progress="skeleton", leaf=LEAF_OVERVIEW)
    if control_id == "skel.map":
        return AdminResult(ok=True, progress="setup_map", leaf=LEAF_OVERVIEW)
    if control_id == "skel.rights":
        return await _reapply(ctx)
    if control_id == "skel.show":
        return await _post_missing(ctx)
    if control_id == "skel.chan.prev":
        _set_cursor(ctx, "chan", _cursor(ctx, "chan") - 1)
        return AdminResult(ok=True, leaf=LEAF_CHANNELS)
    if control_id == "skel.chan.next":
        _set_cursor(ctx, "chan", _cursor(ctx, "chan") + 1)
        return AdminResult(ok=True, leaf=LEAF_CHANNELS)
    if control_id == "skel.role.prev":
        _set_cursor(ctx, "role", _cursor(ctx, "role") - 1)
        return AdminResult(ok=True, leaf=LEAF_ROLES)
    if control_id == "skel.role.next":
        _set_cursor(ctx, "role", _cursor(ctx, "role") + 1)
        return AdminResult(ok=True, leaf=LEAF_ROLES)
    if control_id.startswith("skel.chan.bind:"):
        return await _bind(ctx, control_id.removeprefix("skel.chan.bind:"), value, leaf=LEAF_CHANNELS)
    if control_id.startswith("skel.role.bind:"):
        return await _bind(ctx, control_id.removeprefix("skel.role.bind:"), value, leaf=LEAF_ROLES)
    if control_id.startswith("skel.chan.create:"):
        return await _create_one(ctx, control_id.removeprefix("skel.chan.create:"), leaf=LEAF_CHANNELS)
    if control_id.startswith("skel.role.create:"):
        return await _create_one(ctx, control_id.removeprefix("skel.role.create:"), leaf=LEAF_ROLES)
    if control_id == "skel.notify.bind":
        return await _bind(ctx, "structure", value, leaf=LEAF_NOTIFY)
    if control_id == "skel.notify.toggle":
        return await _toggle_notify(ctx)
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=LEAF_OVERVIEW)


async def _confirm_build(ctx: AdminCtx) -> AdminResult:
    seed = _seed(ctx)
    catalog = getattr(seed, "catalog", None)
    if not callable(catalog) or ctx.guild is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=LEAF_OVERVIEW)
    plan, _existing = await catalog(ctx.guild)
    return AdminResult(
        ok=True,
        confirm="skel.build.yes",
        title=plan_question(plan),
        labels=(copy.SKELETON_BUILD_VERB,),
        leaf=LEAF_OVERVIEW,
    )


async def execute_build(ctx: AdminCtx, *, on_progress: ProgressFn | None = None) -> SeedReport:
    seed = _seed(ctx)
    run = getattr(seed, "run", None)
    catalog = getattr(seed, "catalog", None)
    if not callable(run) or ctx.guild is None:
        return SeedReport(verdict=SeedVerdict.FAILED)
    if not _debounce(ctx):
        return SeedReport(verdict=SeedVerdict.HELD, notes=(copy.SKELETON_BUILD_BUSY,))
    keys: frozenset[str] | None = None
    if callable(catalog):
        plan, _existing = await catalog(ctx.guild)
        keys = _work_keys(plan)
        if not keys:
            return SeedReport(verdict=SeedVerdict.OK)
    return await run(
        ctx.guild,
        actor_id=ctx.actor_id,
        mode=SeedMode.BUILD,
        on_progress=on_progress,
        only_keys=keys,
    )


async def commit_build(ctx: AdminCtx, *, on_progress: ProgressFn | None = None) -> AdminResult:
    from conductor.services.admin_pages import log_change

    report = await execute_build(ctx, on_progress=on_progress)
    if report.notes and report.notes[0] == copy.SKELETON_BUILD_BUSY:
        return AdminResult(ok=False, error=copy.SKELETON_BUILD_BUSY, leaf=LEAF_OVERVIEW)
    changed = int(report.created) or int(report.repaired)
    if not changed:
        return AdminResult(ok=True, leaf=LEAF_OVERVIEW)
    after = str(int(report.created))
    log_change(
        ctx.bot,
        ctx.guild_id,
        page=copy.ADMIN_PAGE_SKELETON,
        field=copy.SKELETON_BUILD,
        before="—",
        after=after,
        actor_id=ctx.actor_id,
    )
    return AdminResult(
        ok=True,
        field=copy.SKELETON_BUILD,
        before="—",
        after=after,
        leaf=LEAF_OVERVIEW,
    )


async def _reapply(ctx: AdminCtx) -> AdminResult:
    from conductor.services.admin_pages import log_change

    reapply = getattr(_seed(ctx), "reapply_rights", None)
    if not callable(reapply) or ctx.guild is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=LEAF_OVERVIEW)
    report = await reapply(ctx.guild, actor_id=ctx.actor_id)
    if not int(report.repaired):
        return AdminResult(ok=True, leaf=LEAF_OVERVIEW)
    after = str(int(report.repaired))
    log_change(
        ctx.bot,
        ctx.guild_id,
        page=copy.ADMIN_PAGE_SKELETON,
        field=copy.DIAGNOSE_REAPPLY,
        before="—",
        after=after,
        actor_id=ctx.actor_id,
    )
    return AdminResult(
        ok=True,
        field=copy.DIAGNOSE_REAPPLY,
        before="—",
        after=after,
        leaf=LEAF_OVERVIEW,
    )


async def _post_missing(ctx: AdminCtx) -> AdminResult:
    from conductor.services.admin_pages import log_change

    poster = getattr(getattr(ctx.bot, "showcase", None), "post_missing", None)
    if not callable(poster) or ctx.guild is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=LEAF_SHOWCASE)
    posted = await poster(ctx.guild, actor_id=ctx.actor_id)
    if not posted:
        return AdminResult(ok=True, leaf=LEAF_SHOWCASE)
    failed = next((item for item in posted if not item.ok), None)
    if failed is not None:
        return AdminResult(ok=False, error=failed.error or copy.STALE_BODY, leaf=LEAF_SHOWCASE)
    after = str(len(posted))
    log_change(
        ctx.bot,
        ctx.guild_id,
        page=copy.ADMIN_PAGE_SKELETON,
        field=copy.SHOWCASE_POST_MISSING,
        before="—",
        after=after,
        actor_id=ctx.actor_id,
    )
    return AdminResult(
        ok=True,
        field=copy.SHOWCASE_POST_MISSING,
        before="—",
        after=after,
        leaf=LEAF_SHOWCASE,
    )


async def _bind(ctx: AdminCtx, key: str, value: str, *, leaf: int) -> AdminResult:
    from conductor.services.admin_pages import log_change

    binder = getattr(_seed(ctx), "bind_existing", None)
    if not callable(binder) or ctx.guild is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=leaf)
    try:
        object_id = int(value)
    except (TypeError, ValueError):
        return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL, leaf=leaf)
    before = await _bound_id(ctx, key)
    await binder(ctx.guild, {key: object_id}, actor_id=ctx.actor_id)
    entry = entry_by_key(key)
    label = entry.diagnose_label if entry is not None else key
    after = str(object_id)
    log_change(
        ctx.bot,
        ctx.guild_id,
        page=copy.ADMIN_PAGE_SKELETON,
        field=label,
        before="—" if before is None else str(before),
        after=after,
        actor_id=ctx.actor_id,
    )
    return AdminResult(ok=True, field=label, before="—" if before is None else str(before), after=after, leaf=leaf)


async def _create_one(ctx: AdminCtx, key: str, *, leaf: int) -> AdminResult:
    from conductor.services.admin_pages import log_change

    repair = getattr(_seed(ctx), "repair", None)
    if not callable(repair) or ctx.guild is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=leaf)
    report = await repair(ctx.guild, key, actor_id=ctx.actor_id, mode=SeedMode.BUILD)
    if not int(report.created):
        return AdminResult(ok=True, leaf=leaf)
    entry = entry_by_key(key)
    label = entry.diagnose_label if entry is not None else key
    after = str(int(report.created))
    log_change(
        ctx.bot,
        ctx.guild_id,
        page=copy.ADMIN_PAGE_SKELETON,
        field=label,
        before="—",
        after=after,
        actor_id=ctx.actor_id,
    )
    return AdminResult(ok=True, field=label, before="—", after=after, leaf=leaf)


async def _toggle_notify(ctx: AdminCtx) -> AdminResult:
    from conductor.db.session import session_scope
    from conductor.services.admin_pages import log_change
    from conductor.services.guild_config import get_or_create_config

    bound = await _bound_id(ctx, "structure")
    async with session_scope(ctx.bot.sessions) as session:
        row = await get_or_create_config(session, ctx.guild_id)
        if bound is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL, leaf=LEAF_NOTIFY)
        row.structure_channel_id = None
    log_change(
        ctx.bot,
        ctx.guild_id,
        page=copy.ADMIN_PAGE_SKELETON,
        field=copy.SKELETON_NOTIFY_TOGGLE,
        before=str(bound),
        after="—",
        actor_id=ctx.actor_id,
    )
    return AdminResult(
        ok=True,
        field=copy.SKELETON_NOTIFY_TOGGLE,
        before=str(bound),
        after="—",
        leaf=LEAF_NOTIFY,
    )


async def _bound_id(ctx: AdminCtx, key: str) -> int | None:
    snapshot = getattr(_seed(ctx), "snapshot", None)
    if not callable(snapshot) or ctx.guild is None:
        return None
    existing = await snapshot(ctx.guild)
    item = existing.get(key)
    if item is None:
        return None
    bound = getattr(item, "bound_id", None)
    if bound and getattr(item, "exists", False):
        return int(bound)
    return None


def _work_keys(plan: SeedPlan) -> frozenset[str]:
    return frozenset((*plan.create, *plan.repair, *plan.by_name))


def _debounce(ctx: AdminCtx) -> bool:
    method = getattr(getattr(ctx.bot, "queue", None), "debounce", None)
    if not callable(method):
        return True
    return bool(method(("skel.build", int(ctx.guild_id)), INGEST_DEBOUNCE))


def _suffix_int(control_id: str, default: int) -> int:
    token = control_id.rsplit(":", 1)[-1]
    try:
        return int(token)
    except ValueError:
        return default


def _channel_types(kind: SkeletonKind) -> tuple[str, ...]:
    if kind is SkeletonKind.VOICE:
        return ("voice",)
    if kind is SkeletonKind.CATEGORY:
        return ("category",)
    return ("text",)


def _role_subsystem(key: str) -> RoleSubsystem:
    if key.startswith("role_sub_"):
        return RoleSubsystem.SELFROLES
    if key.startswith(("role_fixer", "role_ideologist", "role_vigilant")):
        return RoleSubsystem.ACHIEVEMENTS
    if key in {"role_ruby", "role_emerald", "role_sapphire", "role_amethyst"}:
        return RoleSubsystem.LOYALTY
    if key in {"role_friend", "role_resident", "role_regular", "role_guest"}:
        return RoleSubsystem.ACTIVITY
    if key in {
        "role_exemplary",
        "role_respected",
        "role_reliable",
        "role_ordinary",
        "role_suspicious",
        "role_sinner",
    }:
        return RoleSubsystem.REPUTATION
    if key == "role_verified":
        return RoleSubsystem.VERIFICATION
    if key == "role_mute":
        return RoleSubsystem.MODERATION
    return RoleSubsystem.STAFF


PAGE = AdminPage(
    PAGE_SKELETON,
    copy.ADMIN_PAGE_SKELETON,
    SKELETON_READ,
    _ready,
    _snapshot,
    _handle,
    keys={
        "skel.leaf": SKELETON_READ,
        "skel.build": SKELETON_BUILD,
        "skel.map": SKELETON_MANAGE,
        "skel.rights": SKELETON_MANAGE,
        "skel.show": SKELETON_MANAGE,
        "skel.chan": SKELETON_BIND,
        "skel.role": SKELETON_BIND,
        "skel.notify": SKELETON_BIND,
    },
)
