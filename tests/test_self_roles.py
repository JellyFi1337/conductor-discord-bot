from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from discord import ui

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import SelfRoleMenu, VerificationSettings
from conductor.db.session import session_scope
from conductor.domain.access import AccessSnapshot, Rank, empty_rank_roles
from conductor.domain.self_roles import (
    MENU_OPTION_MAX,
    MenuKind,
    OptionTag,
    SelfRoleOption,
    card_fingerprint,
    desired_diff,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import format_custom_id
from conductor.presentation.self_roles import menu_card, pick_card
from conductor.services.role_broker import RoleBindOverlap, RoleClaim, RoleSubsystem
from conductor.services.self_roles import SelfRolesService, channels_opened_by
from conductor.services.verification import VerificationService

DOMAIN_PY = Path(__file__).resolve().parents[1] / "src" / "conductor" / "domain" / "self_roles.py"
GUILD_ID = 1


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


class FakeRoles:
    def __init__(self) -> None:
        self.claims: list[RoleClaim] = []
        self.owners: dict[int, RoleSubsystem] = {}
        self.fail: str | None = None

    def bind(
        self,
        _guild_id: int,
        subsystem: RoleSubsystem,
        role_ids: object,
        *,
        strict: bool = False,
    ) -> frozenset[int]:
        wanted = tuple(int(role_id) for role_id in role_ids)
        if strict:
            for role_id in wanted:
                owner = self.owners.get(role_id)
                if owner is not None and owner is not subsystem:
                    raise RoleBindOverlap(role_id, owner)
        for role_id, owner in list(self.owners.items()):
            if owner is subsystem and role_id not in wanted:
                del self.owners[role_id]
        for role_id in wanted:
            self.owners[role_id] = subsystem
        return frozenset()

    def owner_of(
        self,
        _guild_id: int,
        role_id: int,
        except_subsystem: RoleSubsystem | None = None,
    ) -> RoleSubsystem | None:
        owner = self.owners.get(int(role_id))
        if owner is None or owner is except_subsystem:
            return None
        return owner

    async def apply(self, claim: RoleClaim) -> SimpleNamespace:
        self.claims.append(claim)
        if self.fail:
            return SimpleNamespace(ok=False, error=self.fail)
        return SimpleNamespace(ok=True)


class FakeOverwrite:
    def __init__(self, view_channel: bool | None) -> None:
        self.view_channel = view_channel


class FakeChannel:
    def __init__(self, channel_id: int, name: str, kind: str, *, view: bool | None) -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.type = SimpleNamespace(name=kind)
        self._view = view

    def overwrites_for(self, _role: object) -> FakeOverwrite:
        return FakeOverwrite(self._view)


def _guild(*channels: FakeChannel, roles: dict[int, object] | None = None) -> SimpleNamespace:
    by_id = {int(channel.id): channel for channel in channels}
    role_map = roles or {}
    return SimpleNamespace(
        id=GUILD_ID,
        text_channels=[item for item in channels if item.type.name not in {"voice", "stage_voice", "category"}],
        voice_channels=[item for item in channels if item.type.name == "voice"],
        stage_channels=[item for item in channels if item.type.name == "stage_voice"],
        forums=(),
        channels=list(channels),
        get_channel=lambda channel_id: by_id.get(int(channel_id)),
        get_role=lambda role_id: role_map.get(int(role_id), SimpleNamespace(id=int(role_id))),
    )


async def _runtime(tmp_path: Path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    roles = FakeRoles()
    bot = SimpleNamespace(sessions=sessions, roles=roles, scheduler=None, outbound=None, get_guild=lambda _gid: None)
    return bot, engine, SelfRolesService(bot), roles


async def _add(service: SelfRolesService, menu_id: int, role_id: int, label: str, tag: str = "none") -> str | None:
    return await service.add_option(
        GUILD_ID,
        menu_id,
        role_id,
        label=label,
        description="",
        emoji=None,
        tag=tag,
    )


async def _seed_menu(sessions, *, kind: str = MenuKind.GENERAL.value, title: str = "Подписки") -> int:
    async with session_scope(sessions) as session:
        row = SelfRoleMenu(guild_id=GUILD_ID, kind=kind, title=title)
        session.add(row)
        await session.flush()
        return int(row.id)


def test_domain_has_no_discord_or_sqlalchemy() -> None:
    source = DOMAIN_PY.read_text(encoding="utf-8")
    assert "discord" not in source
    assert "sqlalchemy" not in source


def test_desired_diff_keeps_roles_outside_the_menu() -> None:
    add, remove = desired_diff(have=(10, 20, 99), menu=(10, 20), want=(10,))
    assert add == ()
    assert remove == (20,)
    add, remove = desired_diff(have=(99,), menu=(10, 20), want=(10, 20))
    assert add == (10, 20)
    assert remove == ()
    add, remove = desired_diff(have=(10, 20), menu=(10, 20), want=(10, 20, 30))
    assert add == ()
    assert remove == ()


def test_card_fingerprint_changes_when_opened_channels_change() -> None:
    first = card_fingerprint(kind="game", title="Игры", options=((7, "Forts", "", None, ("#forts",)),))
    second = card_fingerprint(kind="game", title="Игры", options=((7, "Forts", "", None, ("#forts", "🔊 войс")),))
    assert first != second
    assert first == card_fingerprint(kind="game", title="Игры", options=((7, "Forts", "", None, ("#forts",)),))


def test_channels_opened_by_uses_explicit_view_only() -> None:
    role = SimpleNamespace(id=7)
    guild = _guild(
        FakeChannel(1, "forts", "text", view=True),
        FakeChannel(2, "общий", "text", view=None),
        FakeChannel(3, "скрытый", "text", view=False),
        FakeChannel(4, "forts-войс", "voice", view=True),
        FakeChannel(5, "категория", "category", view=True),
    )
    assert channels_opened_by(guild, role) == ("<#1>", "🔊 forts-войс")


def test_menu_card_select_is_full_set() -> None:
    option = SelfRoleOption(option_id=1, menu_id=4, role_id=7, label="Forts", tag=OptionTag.NONE)
    view = menu_card(menu_id=4, title="Игры", kind=MenuKind.GAME, options=(option,))
    selects = [item for item in view.walk_children() if isinstance(item, ui.Select)]
    assert len(selects) == 1
    assert selects[0].custom_id == format_custom_id("sr", "pick", 4)
    assert selects[0].min_values == 0
    assert selects[0].max_values == 1
    empty = menu_card(menu_id=4, title="Игры", kind=MenuKind.GAME, options=())
    assert not [item for item in empty.walk_children() if isinstance(item, ui.Select)]


async def test_defaults_and_tag_helpers(tmp_path: Path) -> None:
    bot, engine, service, _roles = await _runtime(tmp_path)
    try:
        await service.ensure_defaults(GUILD_ID)
        await service.ensure_defaults(GUILD_ID)
        menus = await service.menus(GUILD_ID)
        assert [menu.title for menu in menus] == ["Подписки", "Игры"]
        assert [menu.kind for menu in menus] == [MenuKind.GENERAL.value, MenuKind.GAME.value]
        general = menus[0]
        error = await _add(service, int(general.id), 11, "", tag="youtube")
        assert error is None
        assert await service.role_for_tag(GUILD_ID, "youtube") == 11
        options = await service.options(int(general.id))
        assert options[0].label == "Новые видео"
        assert options[0].tag is OptionTag.YOUTUBE
        game = menus[1]
        await _add(service, int(game.id), 22, "Forts")
        games = await service.game_options(GUILD_ID)
        assert [item.role_id for item in games] == [22]
        lines = await service.member_lines(GUILD_ID, (11, 22, 99))
        assert copy.selfroles_member_line("Подписки", ["Новые видео"]) in lines
        assert copy.selfroles_member_line("Игры", ["Forts"]) in lines
    finally:
        await engine.dispose()


async def test_role_is_unique_across_menus_and_capped_at_25(tmp_path: Path) -> None:
    bot, engine, service, roles = await _runtime(tmp_path)
    try:
        first = await _seed_menu(bot.sessions, title="A")
        second = await _seed_menu(bot.sessions, kind=MenuKind.GAME.value, title="B")
        assert await _add(service, first, 10, "one") is None
        assert await _add(service, second, 10, "dup") == copy.SELFROLES_TAKEN
        for index in range(MENU_OPTION_MAX - 1):
            assert await _add(service, first, 100 + index, f"r{index}") is None
        assert await _add(service, first, 999, "over") == copy.SELFROLES_LIMIT
        assert roles.owner_of(GUILD_ID, 10, except_subsystem=RoleSubsystem.ACTIVITY) is RoleSubsystem.SELFROLES
    finally:
        await engine.dispose()


def test_special_and_foreign_roles_are_rejected() -> None:
    roles = FakeRoles()
    roles.owners[5] = RoleSubsystem.ACTIVITY
    bot = SimpleNamespace(roles=roles)
    service = SelfRolesService(bot)
    ranks = empty_rank_roles()
    ranks[Rank.SPECIAL] = (8,)
    ranks[Rank.WATCHER] = (9,)
    snapshot = AccessSnapshot(rank_roles=ranks, rules={}, special_locked=True)
    guild = SimpleNamespace(
        id=GUILD_ID,
        default_role=SimpleNamespace(id=GUILD_ID),
        get_role=lambda role_id: SimpleNamespace(id=int(role_id), managed=int(role_id) == 3),
    )
    assert service.option_role_error(GUILD_ID, 8, guild=guild, snapshot=snapshot) == copy.SELFROLES_SPECIAL
    assert service.option_role_error(GUILD_ID, 9, guild=guild, snapshot=snapshot) == copy.SELFROLES_STAFF
    assert service.option_role_error(GUILD_ID, 5, guild=guild, snapshot=snapshot) == copy.config_role_taken(
        copy.role_subsystem_label(RoleSubsystem.ACTIVITY.value)
    )
    assert service.option_role_error(GUILD_ID, 3, guild=guild, snapshot=snapshot) == copy.ADMIN_BAD_ROLE
    assert service.option_role_error(GUILD_ID, GUILD_ID, guild=guild, snapshot=snapshot) == copy.ADMIN_BAD_ROLE


async def test_pick_two_then_deselect_one_leaves_outside_roles(tmp_path: Path) -> None:
    bot, engine, service, roles = await _runtime(tmp_path)
    try:
        menu_id = await _seed_menu(bot.sessions)
        await _add(service, menu_id, 10, "A")
        await _add(service, menu_id, 20, "B")
        member = SimpleNamespace(id=5, roles=[SimpleNamespace(id=99)])
        first = await service.pick(SimpleNamespace(id=GUILD_ID), member, menu_id, (10, 20))
        assert len(roles.claims) == 1
        claim = roles.claims[0]
        assert claim.subsystem is RoleSubsystem.SELFROLES
        assert claim.urgent is True
        assert claim.add == (10, 20)
        assert claim.remove == ()
        text = collected_text(first)
        assert "A" in text
        assert "B" in text
        member.roles = [SimpleNamespace(id=10), SimpleNamespace(id=20), SimpleNamespace(id=99)]
        second = await service.pick(SimpleNamespace(id=GUILD_ID), member, menu_id, (10,))
        assert roles.claims[1].add == ()
        assert roles.claims[1].remove == (20,)
        assert 99 not in roles.claims[1].remove
        assert "B" in collected_text(second)
        assert copy.SELFROLES_NONE not in collected_text(second)
    finally:
        await engine.dispose()


async def test_broker_denial_renders_denied_card(tmp_path: Path) -> None:
    bot, engine, service, roles = await _runtime(tmp_path)
    try:
        menu_id = await _seed_menu(bot.sessions)
        await _add(service, menu_id, 10, "A")
        roles.fail = "роль A выше роли бота"
        view = await service.pick(SimpleNamespace(id=GUILD_ID), SimpleNamespace(id=5, roles=()), menu_id, (10,))
        text = collected_text(view)
        assert "роль A выше роли бота" in text
        assert copy.selfroles_result(granted=("A",), removed=()) not in text
    finally:
        await engine.dispose()


async def test_game_pick_lists_opened_channels(tmp_path: Path) -> None:
    bot, engine, service, _roles = await _runtime(tmp_path)
    try:
        menu_id = await _seed_menu(bot.sessions, kind=MenuKind.GAME.value, title="Игры")
        await _add(service, menu_id, 7, "Forts")
        guild = _guild(FakeChannel(3, "forts", "text", view=True), roles={7: SimpleNamespace(id=7)})
        view = await service.pick(guild, SimpleNamespace(id=5, roles=()), menu_id, (7,))
        assert copy.selfroles_opened(("<#3>",)) in collected_text(view)
    finally:
        await engine.dispose()


async def test_onboarding_mentions_self_role_channel(tmp_path: Path) -> None:
    bot, engine, service, _roles = await _runtime(tmp_path)
    try:
        await service.ensure_defaults(GUILD_ID)
        menus = await service.menus(GUILD_ID)
        async with session_scope(bot.sessions) as session:
            row = await session.get(SelfRoleMenu, int(menus[0].id))
            assert row is not None
            row.channel_id = 44
        verification = VerificationService(bot)
        async with session_scope(bot.sessions) as session:
            session.add(VerificationSettings(guild_id=GUILD_ID, channel_id=55, verified_role_id=88))
        member = SimpleNamespace(id=5, roles=[SimpleNamespace(id=88)])
        card = await verification.grant(SimpleNamespace(id=GUILD_ID), member)
        assert "<#44>" in collected_text(card)
    finally:
        await engine.dispose()


async def test_guard_panel_uses_menu_not_live_row(tmp_path: Path) -> None:
    bot, engine, service, _roles = await _runtime(tmp_path)
    try:
        menu_id = await _seed_menu(bot.sessions)
        resource = await service.guard_panel(GUILD_ID, str(menu_id))
        assert resource is not None
        assert resource.closed is False
        assert await service.guard_panel(GUILD_ID, "999") is None
        foreign = await service.guard_panel(2, str(menu_id))
        assert foreign is not None
        assert foreign.closed is True
    finally:
        await engine.dispose()


def test_pick_card_empty_set() -> None:
    view = pick_card(granted=(), removed=())
    assert copy.SELFROLES_NONE in collected_text(view)
