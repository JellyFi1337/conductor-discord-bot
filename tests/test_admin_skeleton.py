from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from conductor.domain.access import (
    SKELETON_BIND,
    SKELETON_BUILD,
    SKELETON_MANAGE,
    AccessSnapshot,
    Rank,
    empty_rank_roles,
)
from conductor.domain.admin import AdminCtx
from conductor.domain.seed import SeedPlan, SeedReport, SeedVerdict
from conductor.presentation import copy
from conductor.presentation.admin import DISCORD_SELECT_LIMIT, page_card
from conductor.presentation.setup_wizard import (
    LEAF_CHANNELS,
    LEAF_ROLES,
    SKELETON_LEAVES,
    action_rows,
    select_option_counts,
)
from conductor.services.admin_pages import all_pages, filter_controls, may_use, page_by_id
from conductor.services.admin_skeleton import PAGE, commit_build


def _ctx(bot: object) -> AdminCtx:
    return AdminCtx(
        bot=bot,
        guild_id=1,
        actor_id=2,
        guild=SimpleNamespace(
            id=1,
            default_role=SimpleNamespace(id=1, name="@everyone", position=0, managed=False),
            me=SimpleNamespace(id=99),
            roles=(),
            channels=(),
            get_channel=lambda _channel_id: None,
            get_role=lambda _role_id: None,
        ),
    )


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def test_skeleton_page_is_first() -> None:
    pages = all_pages()
    assert pages[0] is PAGE
    assert pages[0].id == "skeleton"
    assert page_by_id("skeleton") is PAGE


def test_skeleton_keys_use_separate_tiers() -> None:
    assert PAGE.key == "skeleton.read"
    assert PAGE.keys["skel.leaf"] == "skeleton.read"
    assert PAGE.keys["skel.map"] == SKELETON_MANAGE
    assert PAGE.keys["skel.rights"] == SKELETON_MANAGE
    assert PAGE.keys["skel.show"] == SKELETON_MANAGE
    assert PAGE.keys["skel.build"] == SKELETON_BUILD
    assert PAGE.keys["skel.chan"] == SKELETON_BIND
    assert PAGE.keys["skel.role"] == SKELETON_BIND


async def test_repeat_build_on_ready_server_is_noop() -> None:
    plan = SeedPlan(create=(), reuse=("rules", "verify"), repair=(), by_name=())
    run = AsyncMock(return_value=SeedReport(verdict=SeedVerdict.OK, created=7))
    bot = SimpleNamespace(
        seed=SimpleNamespace(catalog=AsyncMock(return_value=(plan, {})), run=run),
        queue=SimpleNamespace(debounce=lambda *_args, **_kwargs: True),
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: None),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=99)},
    )
    ctx = _ctx(bot)
    with patch("conductor.services.admin_pages.log_change") as logged:
        result = await commit_build(ctx)
    assert result.ok is True
    assert result.field is None
    run.assert_not_called()
    logged.assert_not_called()


async def test_leaf_handle_returns_same_leaf() -> None:
    ctx = _ctx(SimpleNamespace())
    switched = await PAGE.handle(ctx, "skel.leaf:3", "")
    assert switched.ok is True
    assert switched.leaf == LEAF_ROLES
    moved = await PAGE.handle(ctx, "skel.chan.next", "")
    assert moved.leaf == LEAF_CHANNELS
    roles = await PAGE.handle(ctx, "skel.role.prev", "")
    assert roles.leaf == LEAF_ROLES


async def test_skeleton_leaves_fit_layout_budget() -> None:
    bot = SimpleNamespace(
        seed=SimpleNamespace(
            diagnose_checks=AsyncMock(return_value=()),
            snapshot=AsyncMock(return_value={}),
        ),
        showcase=SimpleNamespace(
            statuses=AsyncMock(return_value={}),
            bound_ids=AsyncMock(return_value={}),
        ),
        roles=SimpleNamespace(owner_of=lambda *_args, **_kwargs: None),
    )
    ctx = _ctx(bot)
    for leaf in range(1, SKELETON_LEAVES + 1):
        snap = await PAGE.snapshot(ctx, leaf)
        assert snap.leaf == leaf
        view = page_card(
            title=PAGE.label,
            fields=snap.fields,
            footnote=snap.footnote,
            controls=snap.controls,
            on_control=_noop,
            on_home=_noop,
        )
        assert 1 <= len(action_rows(view)) <= 5
        assert all(count <= DISCORD_SELECT_LIMIT for count in select_option_counts(view))


def test_manage_buttons_hidden_without_key() -> None:
    ranks = empty_rank_roles()
    ranks[Rank.SENIOR] = (3,)
    snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    controls = (
        SimpleNamespace(id="skel.leaf:1", label="x"),
        SimpleNamespace(id="skel.build", label=copy.SKELETON_BUILD),
        SimpleNamespace(id="skel.map", label=copy.SETUP_MAP_OPEN),
        SimpleNamespace(id="skel.rights", label=copy.DIAGNOSE_REAPPLY),
        SimpleNamespace(id="skel.show", label=copy.SHOWCASE_POST_MISSING),
    )
    filtered = filter_controls(snapshot, PAGE, controls, member_role_ids=(3,))  # type: ignore[arg-type]
    allowed = {item.id for item in filtered.allowed}
    assert "skel.leaf:1" in allowed
    assert "skel.build" not in allowed
    assert "skel.map" not in allowed
    assert "skel.rights" not in allowed
    assert "skel.show" not in allowed
    denied = may_use(snapshot, PAGE, "skel.rights", member_role_ids=(3,))
    assert denied.allowed is False
    denied_build = may_use(snapshot, PAGE, "skel.build", member_role_ids=(3,))
    assert denied_build.allowed is False


def test_chief_sees_manage_not_bind() -> None:
    ranks = empty_rank_roles()
    ranks[Rank.CHIEF] = (7,)
    snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    assert may_use(snapshot, PAGE, "skel.map", member_role_ids=(7,)).allowed is True
    assert may_use(snapshot, PAGE, "skel.build", member_role_ids=(7,)).allowed is True
    assert may_use(snapshot, PAGE, "skel.chan.bind:rules", member_role_ids=(7,)).allowed is False
