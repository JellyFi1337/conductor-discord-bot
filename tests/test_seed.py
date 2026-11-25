from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import Base, GuildConfig, GuildLayout, GuildRank, ModSettings
from conductor.db.session import session_scope
from conductor.domain.create_lock import BUCKET_CHANNEL, HoldSource
from conductor.domain.guilds import GuildFence
from conductor.domain.permissions import REQUIRED_PERMISSIONS
from conductor.domain.seed import (
    _ROLE_ORDER,
    BINDING_EXCEPTIONS,
    CHANNEL_PAGES,
    FEATURES,
    LAYOUT,
    ROLE_GROUPS,
    ROLE_PAGES,
    SEED_NEEDS_CONFIRMATION,
    SEED_ROLE_ALIASES,
    SEED_STAGES,
    SKELETON,
    BlueprintStatus,
    ExistingItem,
    SeedGate,
    SeedMode,
    SeedReport,
    SeedVerdict,
    SkeletonKind,
    blueprint_status,
    colour_hex,
    creatable_keys,
    dependency_order,
    entries_for_feature,
    entries_in_category,
    entry_by_key,
    feature_of,
    has_seed_permissions,
    has_seed_role_headroom,
    is_same_name,
    match_skeleton,
    needs_connect,
    plan,
    required_keys,
    rights_key,
    role_order,
    seed_gate,
    seed_guest_plan,
    seed_role_aliases,
    skeleton_entries,
    skeleton_setting_fields,
)
from conductor.presentation import copy
from conductor.presentation.layout import TEXT_LIMIT, collected_text
from conductor.presentation.ops import seed_blueprint, seed_done
from conductor.services.cache import CacheHub
from conductor.services.census import CensusResult
from conductor.services.create_lock import before_mutate, remember
from conductor.services.role_broker import RoleApplyResult, RoleSubsystem
from conductor.services.seed import GuildSeed, _gate_targets, _overwrites_match
from conductor.services.showcase import ShowcaseService


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def test_plan_empty_creates_all() -> None:
    existing = {entry.key: ExistingItem() for entry in skeleton_entries()}
    result = plan(existing)
    assert set(result.create) == {entry.key for entry in skeleton_entries()}
    assert result.reuse == ()
    assert result.repair == ()


def test_plan_present_reuses() -> None:
    existing = {entry.key: ExistingItem(bound_id=1, exists=True) for entry in skeleton_entries()}
    result = plan(existing)
    assert result.create == ()
    assert set(result.reuse) == {entry.key for entry in skeleton_entries()}
    assert result.repair == ()


def test_plan_deleted_rebounds() -> None:
    existing = {entry.key: ExistingItem(bound_id=1, exists=True) for entry in skeleton_entries()}
    existing["automod"] = ExistingItem(bound_id=9, exists=False)
    result = plan(existing)
    assert "automod" in result.create
    assert "automod" not in result.reuse


def test_plan_renamed_reuses() -> None:
    existing = {entry.key: ExistingItem(bound_id=1, exists=True) for entry in skeleton_entries()}
    existing["staff_feed"] = ExistingItem(bound_id=4, exists=True, name="staff-log")
    result = plan(existing)
    assert "staff_feed" in result.reuse
    assert "staff_feed" not in result.create


def test_plan_bad_overwrites_repairs() -> None:
    existing = {entry.key: ExistingItem(bound_id=1, exists=True) for entry in skeleton_entries()}
    existing["cat_server"] = ExistingItem(bound_id=2, exists=True, overwrites_ok=False)
    result = plan(existing)
    assert "cat_server" in result.repair


def test_plan_role_position_is_not_repair() -> None:
    existing = {entry.key: ExistingItem(bound_id=1, exists=True) for entry in skeleton_entries()}
    existing["role_watcher"] = ExistingItem(bound_id=2, exists=True, position_ok=False)
    result = plan(existing)
    assert "role_watcher" not in result.repair
    assert "role_watcher" in result.reuse

    existing["automod"] = ExistingItem(bound_id=3, exists=True, position_ok=False)
    result = plan(existing)
    assert "automod" in result.repair


def test_plan_role_colour_is_repair() -> None:
    existing = {entry.key: ExistingItem(bound_id=1, exists=True) for entry in skeleton_entries()}
    existing["role_special"] = ExistingItem(bound_id=2, exists=True, colour_ok=False)
    result = plan(existing)
    assert "role_special" in result.repair
    assert needs_connect(result) is True


def test_match_skeleton_and_needs_connect() -> None:
    special = next(entry for entry in skeleton_entries() if entry.key == "role_special")
    rules = next(entry for entry in skeleton_entries() if entry.key == "rules")
    groups = next(entry for entry in skeleton_entries() if entry.key == "cat_groups")
    assert match_skeleton(SkeletonKind.ROLE, "Special") is special
    assert match_skeleton(SkeletonKind.TEXT, "правила") is rules
    assert match_skeleton(SkeletonKind.ROLE, "правила") is None
    assert match_skeleton(SkeletonKind.CATEGORY, "группы") is groups
    empty = {entry.key: ExistingItem() for entry in skeleton_entries()}
    assert needs_connect(plan(empty)) is False
    empty["rules"] = ExistingItem(found_by_name=True, exists=True)
    assert needs_connect(plan(empty)) is True


def test_overwrites_match_checks_gate_roles() -> None:
    everyone = SimpleNamespace(id=1)
    verified = SimpleNamespace(id=2)
    watcher = SimpleNamespace(id=3)
    special = SimpleNamespace(id=4)
    chief = SimpleNamespace(id=5)
    senior = SimpleNamespace(id=6)
    roles = {1: everyone, 2: verified, 3: watcher, 4: special, 5: chief, 6: senior}
    guild = SimpleNamespace(default_role=everyone, get_role=roles.get)
    ids = {
        "role_verified": 2,
        "role_watcher": 3,
        "role_special": 4,
        "role_chief": 5,
        "role_senior": 6,
    }
    hidden = SimpleNamespace(view_channel=False)
    seen = SimpleNamespace(view_channel=True, read_message_history=True)
    chief_seen = SimpleNamespace(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        manage_messages=True,
    )
    server = next(entry for entry in skeleton_entries() if entry.key == "cat_server")
    tickets = next(entry for entry in skeleton_entries() if entry.key == "cat_tickets")
    evidence = next(entry for entry in skeleton_entries() if entry.key == "cat_evidence")
    archive = next(entry for entry in skeleton_entries() if entry.key == "cat_group_archive")
    staffed = {1: hidden, 2: seen, 3: seen, 4: seen, 5: seen, 6: seen}
    assert _overwrites_match(guild, SimpleNamespace(overwrites={1: hidden}), server, ids) is False
    assert _overwrites_match(guild, SimpleNamespace(overwrites=staffed), server, ids) is True
    assert {role.id for role in _gate_targets(guild, server, ids)} == {2, 3, 4, 5, 6}
    assert _overwrites_match(
        guild, SimpleNamespace(overwrites={1: hidden, 3: seen, 4: seen, 5: seen, 6: seen}), tickets, ids
    )
    assert _overwrites_match(
        guild,
        SimpleNamespace(overwrites={1: hidden, 4: chief_seen, 5: chief_seen, 3: hidden}),
        evidence,
        ids,
    )
    assert {role.id for role in _gate_targets(guild, evidence, ids)} == {4, 5}
    assert {role.id for role in _gate_targets(guild, archive, ids)} == {4, 5}


def test_blueprint_status_and_rights() -> None:
    assert colour_hex(0xC45CFF) == "#C45CFF"
    rules = next(entry for entry in skeleton_entries() if entry.key == "rules")
    verify = next(entry for entry in skeleton_entries() if entry.key == "verify")
    special = next(entry for entry in skeleton_entries() if entry.key == "role_special")
    assert rights_key(rules) == "public_ro"
    assert rights_key(verify) == "public_rw"
    assert rights_key(special) == "role"
    assert blueprint_status(rules, ExistingItem()) is BlueprintStatus.MISSING
    assert blueprint_status(rules, ExistingItem(found_by_name=True, exists=True)) is BlueprintStatus.FOUND
    assert blueprint_status(rules, ExistingItem(bound_id=1, exists=True)) is BlueprintStatus.BOUND
    assert blueprint_status(rules, ExistingItem(bound_id=1, exists=True, overwrites_ok=False)) is BlueprintStatus.REPAIR


def test_role_order_top_down() -> None:
    order = role_order()
    assert order[0] == "role_special"
    assert order[1] == "role_chief"
    assert "role_ruby" not in order
    assert role_order(with_old=True)[4] == "role_ruby"
    assert order[-1] == "role_mute"
    assert order[-2] == "role_verified"


def test_skeleton_covers_setting_fields() -> None:
    found: set[str] = set()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name.endswith("_channel_id") or column.name.endswith("_role_id"):
                found.add(f"{table.name}.{column.name}")
    missing = found - skeleton_setting_fields() - BINDING_EXCEPTIONS
    assert not missing, sorted(missing)


def test_dependency_order_category_before_children() -> None:
    sample = ("evidence_archive", "role_mute", "cat_evidence", "structure", "cat_staff", "hub")
    for perm in permutations(sample):
        ordered = dependency_order(perm)
        assert ordered.index("cat_evidence") < ordered.index("evidence_archive")
        assert ordered.index("cat_staff") < ordered.index("structure")
        assert ordered[-1] == "role_mute"
        assert dependency_order(reversed(perm)) == ordered

    keys = [entry.key for entry in skeleton_entries()]
    shuffled = list(keys)
    random.Random(0).shuffle(shuffled)
    ordered = dependency_order(shuffled)
    assert ordered == dependency_order(reversed(shuffled))
    by_key = {entry.key: entry for entry in SKELETON}
    ranks = []
    for key in ordered:
        kind = by_key[key].kind
        if kind is SkeletonKind.CATEGORY:
            ranks.append(0)
        elif kind is SkeletonKind.ROLE:
            ranks.append(2)
        else:
            ranks.append(1)
    assert ranks == sorted(ranks)
    for key in ordered:
        parent = by_key[key].category
        if parent and parent in ordered:
            assert ordered.index(parent) < ordered.index(key)


def test_entries_for_feature_evidence() -> None:
    keys = {entry.key for entry in entries_for_feature("evidence")}
    assert keys == {"cat_evidence", "evidence_archive"}
    assert feature_of("evidence_archive") == "evidence"
    assert feature_of("structure") == "core"


def test_required_keys_excludes_old_unless_asked() -> None:
    base = required_keys()
    with_old = required_keys(with_old=True)
    assert "role_ruby" not in base
    assert "role_amethyst" not in base
    assert "role_ruby" in with_old
    assert "role_amethyst" in with_old
    assert "evidence_archive" in base
    assert "structure" in base
    assert creatable_keys() >= {"evidence_archive", "structure", "cat_evidence"}


def test_skeleton_features_are_complete() -> None:
    owned = [key for _feature, _copy, keys in FEATURES for key in keys]
    assert len(owned) == len(set(owned))
    assert {entry.key for entry in SKELETON} == set(owned)
    assert all(entry.feature for entry in SKELETON)
    listed = {feature for feature, _copy, _keys in FEATURES}
    assert {entry.feature for entry in SKELETON} == listed
    for entry in SKELETON:
        assert feature_of(entry.key) == entry.feature
    ruby = next(entry for entry in SKELETON if entry.key == "role_ruby")
    assert ruby.optional is True
    assert ruby.required is True


def test_skeleton_keys_listed_on_pages() -> None:
    channel_hits: dict[str, int] = {}
    for page in CHANNEL_PAGES:
        seen: set[str] = set()
        for cat_key in page:
            seen.update(entry.key for entry in entries_in_category(cat_key, with_old=True))
        for key in seen:
            channel_hits[key] = channel_hits.get(key, 0) + 1
    groups = dict(ROLE_GROUPS)
    role_hits: dict[str, int] = {}
    for page in ROLE_PAGES:
        seen: set[str] = set()
        for group in page:
            seen.update(groups.get(group, ()))
        for key in seen:
            role_hits[key] = role_hits.get(key, 0) + 1
    skeleton_keys = {entry.key for entry in SKELETON}
    roles = {entry.key for entry in SKELETON if entry.kind is SkeletonKind.ROLE}
    channels = skeleton_keys - roles
    assert set(channel_hits) == channels
    assert all(count == 1 for count in channel_hits.values())
    assert set(role_hits) == roles
    assert all(count == 1 for count in role_hits.values())
    assert set(_ROLE_ORDER) == roles
    assert len(_ROLE_ORDER) == len(roles)
    assert set(role_order(with_old=True)) == roles


def test_seed_gate_and_names() -> None:
    assert SEED_NEEDS_CONFIRMATION is True
    assert seed_gate(guild_id=5, served=False) is SeedGate.NOT_SERVED
    assert seed_gate(guild_id=1, served=True) is SeedGate.OK
    flags = {item.attr: True for item in REQUIRED_PERMISSIONS}
    assert has_seed_permissions(SimpleNamespace(**flags))
    flags["kick_members"] = False
    assert not has_seed_permissions(SimpleNamespace(**flags))
    assert has_seed_role_headroom(bot_top_position=10, blocking_positions=(1, 2))
    assert not has_seed_role_headroom(bot_top_position=5, blocking_positions=(5,))
    assert seed_role_aliases(LAYOUT.role_special) == ("Special",)
    assert set(SEED_ROLE_ALIASES) == {
        LAYOUT.role_special,
        LAYOUT.role_chief,
        LAYOUT.role_senior,
        LAYOUT.role_watcher,
    }
    assert seed_guest_plan(
        members=((11, False, frozenset()), (12, True, frozenset()), (13, False, frozenset({70}))),
        guest_role_id=70,
        ladder_role_ids=(70, 71),
    ) == (11,)
    assert is_same_name("Special", "special")
    assert len(SEED_STAGES) == len(copy.OWNER_SEED_STAGES)
    assert copy.OWNER_SEED_STAGES[0] == "права бота"
    assert copy.seed_stage_label(SEED_STAGES[0]) == "права бота"
    assert copy.RANK_LABELS["special"] == LAYOUT.role_special
    assert LAYOUT.role_special == "Специальный Следящий"
    specs = {spec.name: spec for spec in LAYOUT.role_specs()}
    assert specs[LAYOUT.role_special].colour == 0xC45CFF
    assert specs[LAYOUT.role_mute].hoist is False
    assert "общий" not in LAYOUT.text_names()
    assert LAYOUT.cat_entry == "🚪・Вход"
    # Прежнее имя осталось алиасом: сервер, собранный старой версией, узнаётся.
    assert "Вход" in entry_by_key("cat_entry").aliases
    keys = [entry.key for entry in SKELETON]
    assert len(keys) == len(set(keys))


def test_seed_blueprint_shows_hold_reason_and_deadline_with_bounded_text() -> None:
    existing = {entry.key: ExistingItem() for entry in skeleton_entries()}
    reason = "Создание каналов отложено: ожидание Discord 9 ч 42 мин."
    deadline = "Достигнут предел времени сборки: 180 секунд."
    report = SeedReport(
        verdict=SeedVerdict.PARTIAL,
        created=1,
        held=("cat_server",),
        notes=(reason, *("x" * 1000 + str(i) for i in range(10)), deadline),
    )
    first = collected_text(seed_blueprint(plan(existing), existing=existing, report=report))
    assert reason in first
    assert deadline in first
    assert "Создано сейчас: 1 · отложено 1 · ошибок 0." in first
    for page in range(1, 12):
        view = seed_blueprint(plan(existing), existing=existing, report=report, page=page)
        assert len(collected_text(view)) < TEXT_LIMIT


def test_seed_cards_fit_layout() -> None:
    async def _noop(_click: object) -> None:
        return None

    existing = {entry.key: ExistingItem() for entry in skeleton_entries()}
    preview = seed_blueprint(plan(existing), existing=existing, can_bind=False, on_bind=_noop)
    text = collected_text(preview)
    assert copy.OWNER_SEED_TITLE in text
    assert "TEST_GUILD_ID" not in text
    assert copy.SEED_GROUP_HOW in text
    assert copy.SEED_LIMITS_BODY.split(".")[0] in text
    assert LAYOUT.role_special not in text
    assert len(text) < TEXT_LIMIT
    entry = seed_blueprint(plan(existing), existing=existing, page=2)
    entry_text = collected_text(entry)
    assert copy.SEED_RIGHTS_PUBLIC_RO in entry_text
    assert copy.seed_why("rules") in entry_text
    assert LAYOUT.role_special not in entry_text
    assert len(entry_text) < TEXT_LIMIT
    roles = seed_blueprint(plan(existing), existing=existing, page=7, can_bind=True, on_bind=_noop)
    role_text = collected_text(roles)
    assert copy.OWNER_SEED_VERB in role_text
    assert "#C45CFF" in role_text
    assert LAYOUT.role_special in role_text
    assert copy.seed_why("role_special") in role_text
    assert len(role_text) < TEXT_LIMIT
    done = seed_done(
        SeedReport(
            verdict=SeedVerdict.OK,
            created=10,
            reused=0,
            repaired=0,
            created_roles=15,
            special_granted=True,
            humans=1,
        )
    )
    body = collected_text(done)
    assert copy.OWNER_SEED_TITLE in body
    assert "владельцу сервера" in body
    assert "Special" not in body
    assert len(body) < TEXT_LIMIT


class _Perms:
    def __init__(self, **flags: bool) -> None:
        for item in REQUIRED_PERMISSIONS:
            setattr(self, item.attr, flags.get(item.attr, True))


class _Fake:
    _next_id = 1000

    def __init__(self, name: str, **attrs: object) -> None:
        _Fake._next_id += 1
        self.id = _Fake._next_id
        self.name = name
        self.overwrites: dict[object, object] = {}
        for key, value in attrs.items():
            setattr(self, key, value)

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def set_permissions(self, target: object, overwrite: object = None, **_kwargs: object) -> None:
        self.overwrites[getattr(target, "id", id(target))] = overwrite

    async def delete(self, **_kwargs: object) -> None:
        guild = getattr(self, "_guild", None)
        if guild is None:
            return
        for attr in ("roles", "categories", "text_channels", "voice_channels"):
            bag = getattr(guild, attr, None)
            if isinstance(bag, list) and self in bag:
                bag.remove(self)
                return


class _FakeRole(_Fake):
    def __init__(
        self,
        name: str,
        *,
        managed: bool = False,
        hoist: bool = False,
        colour: int = 0,
        position: int = 1,
    ) -> None:
        super().__init__(
            name,
            managed=managed,
            hoist=hoist,
            colour=SimpleNamespace(value=int(colour)),
            position=position,
        )

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    async def edit(self, **kwargs: object) -> None:
        if "name" in kwargs:
            self.name = str(kwargs["name"])
        if "colour" in kwargs:
            colour = kwargs["colour"]
            self.colour = SimpleNamespace(value=int(getattr(colour, "value", colour) or 0))
        if "hoist" in kwargs:
            self.hoist = bool(kwargs["hoist"])


class _FakeChannel(_Fake):
    def __init__(self, name: str, *, category: _Fake | None = None, kind: str = "text") -> None:
        super().__init__(
            name,
            category_id=None if category is None else category.id,
            category=category,
            topic=None,
            members=[],
            overwrites={},
            type=SimpleNamespace(name=kind),
        )

    async def send(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=self.id + 50_000)

    async def set_permissions(self, target: object, overwrite: object = None, **_kwargs: object) -> None:
        self.overwrites[getattr(target, "id", id(target))] = overwrite

    async def edit(self, **kwargs: object) -> None:
        if "name" in kwargs:
            self.name = str(kwargs["name"])
        if "category" in kwargs:
            category = kwargs["category"]
            self.category = category
            self.category_id = None if category is None else getattr(category, "id", None)
        if "topic" in kwargs:
            self.topic = kwargs["topic"]


class _FakeVoice(_FakeChannel):
    def __init__(self, name: str, *, category: _Fake | None = None) -> None:
        super().__init__(name, category=category, kind="voice")


class _FakeMember:
    def __init__(self, user_id: int, *, bot: bool = False, roles: list[_FakeRole] | None = None) -> None:
        self.id = user_id
        self.bot = bot
        self.roles = list(roles or ())


class _FakeGuild:
    def __init__(self, guild_id: int, *, perms: _Perms | None = None, owner_id: int = 7) -> None:
        self.id = guild_id
        self.owner_id = owner_id
        self.default_role = _FakeRole("@everyone", position=0)
        self.bot_role = _FakeRole("Conductor", position=100)
        self.roles = [self.default_role, self.bot_role]
        self.categories: list[_Fake] = []
        self.text_channels: list[_FakeChannel] = []
        self.voice_channels: list[_FakeVoice] = []
        self.members: list[_FakeMember] = []
        self.me = SimpleNamespace(id=1, guild_permissions=perms or _Perms(), top_role=self.bot_role)
        self.position_edits: list[dict[object, int]] = []

    def get_member(self, user_id: int) -> _FakeMember | None:
        return next((member for member in self.members if member.id == user_id), None)

    def get_channel(self, channel_id: int) -> object | None:
        for item in (*self.categories, *self.text_channels, *self.voice_channels):
            if item.id == channel_id:
                return item
        return None

    def get_role(self, role_id: int) -> _FakeRole | None:
        return next((role for role in self.roles if role.id == role_id), None)

    async def edit_role_positions(self, positions: dict[object, int], reason: str | None = None) -> None:
        self.position_edits.append(dict(positions))
        for role, position in positions.items():
            getattr(role, "position", None)
            role.position = position  # type: ignore[attr-defined]

    async def create_role(self, name: str, **kwargs: object) -> _FakeRole:
        colour = kwargs.get("colour") or kwargs.get("color")
        role = _FakeRole(
            name,
            hoist=bool(kwargs.get("hoist")),
            colour=int(getattr(colour, "value", colour) or 0),
            position=len(self.roles),
        )
        role._guild = self
        self.roles.append(role)
        return role

    async def create_category(self, name: str, **_kwargs: object) -> _Fake:
        found = _Fake(name)
        found._guild = self
        self.categories.append(found)
        return found

    async def create_text_channel(self, name: str, **kwargs: object) -> _FakeChannel:
        found = _FakeChannel(name, category=kwargs.get("category"))
        found.topic = kwargs.get("topic")
        found._guild = self
        self.text_channels.append(found)
        return found

    async def create_voice_channel(self, name: str, **kwargs: object) -> _FakeVoice:
        found = _FakeVoice(name, category=kwargs.get("category"))
        found._guild = self
        self.voice_channels.append(found)
        return found


async def _plant_skeleton(
    guild: _FakeGuild, *, with_old: bool = False, names: dict[str, str] | None = None
) -> None:
    cats: dict[str, _Fake] = {}
    for entry in skeleton_entries(with_old=with_old):
        title = names.get(entry.key, entry.name) if names else entry.name
        if entry.kind is SkeletonKind.CATEGORY:
            found = await guild.create_category(title)
            cats[entry.key] = found
        elif entry.kind is SkeletonKind.ROLE:
            await guild.create_role(name=title, colour=SimpleNamespace(value=entry.colour), hoist=entry.hoist)
        elif entry.kind is SkeletonKind.VOICE:
            await guild.create_voice_channel(title, category=cats.get(entry.category or ""))
        else:
            channel = await guild.create_text_channel(title, category=cats.get(entry.category or ""))
            channel.topic = entry.topic


def _ticket_mocks() -> SimpleNamespace:
    live = {"ok": False}

    async def panel_is_live(_guild: object, _channel: object) -> bool:
        return live["ok"]

    async def post_panel(*_args: object, **_kwargs: object) -> bool:
        live["ok"] = True
        return True

    return SimpleNamespace(
        panel_is_live=AsyncMock(side_effect=panel_is_live),
        post_panel=AsyncMock(side_effect=post_panel),
    )


def _bot(sessions: object, guild: _FakeGuild) -> SimpleNamespace:
    fence = GuildFence(guild_ids=frozenset({1, 99}))
    roles = SimpleNamespace(
        bind=MagicMock(),
        apply=AsyncMock(return_value=RoleApplyResult(ok=True)),
        owned=MagicMock(return_value=frozenset()),
        owner_of=MagicMock(return_value=None),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(test_guild_id=99, public_guild_id=1, data_dir=None),
        fence=fence,
        serves=fence.serves,
        sessions=sessions,
        caches=CacheHub(),
        config_by_guild={},
        roles=roles,
        rooms=SimpleNamespace(
            is_room_channel=AsyncMock(return_value=False),
            set_hub=AsyncMock(),
            configure_preset=AsyncMock(return_value=SimpleNamespace(template="{n} · {username}")),
        ),
        tickets=_ticket_mocks(),
        lfg=SimpleNamespace(
            card_status=AsyncMock(return_value="off"),
            set_channel=AsyncMock(),
            post_starter=AsyncMock(return_value=True),
        ),
        loyalty=SimpleNamespace(protect_guild=AsyncMock()),
        ladder=SimpleNamespace(update_config=AsyncMock(return_value=object())),
        reputation=SimpleNamespace(set_tier_role=AsyncMock(return_value=object())),
        achievements=SimpleNamespace(set_role=AsyncMock(return_value=object())),
        moderation=SimpleNamespace(set_mute_role=AsyncMock()),
        verification=SimpleNamespace(bind=AsyncMock(), post_cards=AsyncMock(return_value=True)),
        self_roles=SimpleNamespace(
            ensure_defaults=AsyncMock(),
            bind=AsyncMock(),
            menus=AsyncMock(return_value=()),
            post_menu=AsyncMock(return_value=None),
        ),
        automod=SimpleNamespace(card_status=AsyncMock(return_value="off"), post_card=AsyncMock(return_value=True)),
        stats_board=SimpleNamespace(
            card_status=AsyncMock(return_value="off"),
            set_channel=AsyncMock(),
            post=AsyncMock(return_value=True),
        ),
        poll=SimpleNamespace(set_channel=AsyncMock(), set_ping_role=AsyncMock()),
        refresh_config=AsyncMock(),
        run_census=AsyncMock(return_value=CensusResult(total=2, humans=1, bots=1, boosters=0, created=1, updated=0)),
        get_guild=lambda guild_id: guild if int(guild_id) == guild.id else None,
    )
    bot.showcase = ShowcaseService(bot)
    return bot


async def test_seed_refuses_unserved_and_missing_perms(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        foreign = _FakeGuild(5)
        bot = _bot(sessions, foreign)
        service = GuildSeed(bot)
        denied = await service.run(foreign, actor_id=7)
        assert denied.verdict is SeedVerdict.NOT_SERVED
        assert service.allowed(5) is SeedGate.NOT_SERVED
        assert service.allowed(99) is SeedGate.OK
        assert service.allowed(1) is SeedGate.OK
        assert not service.bot_can_seed(_FakeGuild(99, perms=_Perms(manage_roles=False)))
        assert not service.bot_can_seed(_FakeGuild(99, perms=_Perms(kick_members=False)))
        high = _FakeGuild(99)
        high.roles.append(_FakeRole("Special", position=200))
        assert not service.bot_can_seed(high)
        blocked = await service.run(_FakeGuild(99, perms=_Perms(ban_members=False)), actor_id=7)
        assert blocked.verdict is SeedVerdict.NO_PERMS
    finally:
        await engine.dispose()


async def test_seed_builds_and_reuses(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99, owner_id=21)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    stages: list[tuple[str, bool]] = []

    async def on_progress(name: str, ok: bool) -> None:
        stages.append((name, ok))

    try:
        empty = await service.run(guild, actor_id=7, mode=SeedMode.ADOPT)
        assert empty.verdict is SeedVerdict.PARTIAL
        assert empty.created == 0
        assert empty.created_roles == 0
        assert empty.created_channels == 0
        assert empty.missing
        assert any("вручную" in note for note in empty.notes)
        await _plant_skeleton(guild)
        report = await service.run(guild, actor_id=7, on_progress=on_progress)
        assert report.verdict is SeedVerdict.OK
        assert report.created_roles == 0
        assert report.reused_roles == len([entry for entry in skeleton_entries() if entry.kind is SkeletonKind.ROLE])
        assert report.created_channels == 0
        assert report.reused_channels == len(
            [entry for entry in skeleton_entries() if entry.kind is not SkeletonKind.ROLE]
        )
        assert report.special_granted is True
        assert report.humans == 1
        assert stages == [(name, True) for name in copy.OWNER_SEED_STAGES]
        wanted_roles = {entry.name for entry in skeleton_entries() if entry.kind is SkeletonKind.ROLE}
        assert {role.name for role in guild.roles} >= wanted_roles
        assert {channel.name for channel in guild.text_channels} >= set(LAYOUT.text_names())
        assert "общий" not in {channel.name for channel in guild.text_channels}
        assert len(guild.categories) == 9
        assert guild.position_edits
        claims = [call.args[0] for call in bot.roles.apply.await_args_list]
        owner_claim = next(claim for claim in claims if claim.subsystem is RoleSubsystem.OWNER)
        assert owner_claim.user_id == 21
        async with session_scope(sessions) as session:
            config = await session.get(GuildConfig, 99)
            ranks = (await session.scalars(select(GuildRank).where(GuildRank.guild_id == 99))).all()
            layout = (await session.scalars(select(GuildLayout).where(GuildLayout.guild_id == 99))).all()
        assert config is not None
        assert config.user_log_channel_id
        assert config.staff_feed_channel_id
        assert {row.rank for row in ranks} == {"special", "chief", "senior", "watcher"}
        assert {row.key for row in layout} >= {"cat_entry", "cat_server", "video", "rules"}
        edits = len(guild.position_edits)
        idle = await service.run(guild, actor_id=7)
        assert idle.verdict is SeedVerdict.OK
        assert len(guild.position_edits) == edits
        special = next(role for role in guild.roles if role.name == LAYOUT.role_special)
        special.colour = SimpleNamespace(value=0)
        again = await service.run(guild, actor_id=7)
        assert again.verdict is SeedVerdict.OK
        assert again.created == 0
        assert again.created_roles == 0
        assert again.created_channels == 0
        assert int(getattr(special.colour, "value", 0)) == 0xC45CFF
        assert bot.tickets.post_panel.await_count == 1
        staff = next(item for item in guild.text_channels if item.name == LAYOUT.staff_feed)
        staff.name = "staff-log"
        renamed = await service.run(guild, actor_id=7)
        assert renamed.created_channels == 0
        staff_names = [
            item.name for item in guild.text_channels if "staff" in item.name or item.name == LAYOUT.staff_feed
        ]
        assert len(staff_names) == 1
    finally:
        await engine.dispose()


async def test_seed_with_old_and_repair_automod(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        await _plant_skeleton(guild, with_old=True)
        first = await service.run(guild, actor_id=7, with_old=True)
        assert first.verdict is SeedVerdict.OK
        assert {role.name for role in guild.roles} >= {LAYOUT.role_ruby, LAYOUT.role_amethyst}
        automod = next(item for item in guild.text_channels if item.name == LAYOUT.automod)
        automod_id = automod.id
        guild.text_channels.remove(automod)
        repaired = await service.repair(guild, "automod", actor_id=7)
        assert repaired.created_channels == 1
        assert "automod" not in repaired.missing
        created = next(item for item in guild.text_channels if item.name == LAYOUT.automod)
        assert created.id != automod_id
        bound = await service.repair(guild, "automod", actor_id=7)
        assert bound.created_channels == 0
        again = next(item for item in guild.text_channels if item.name == LAYOUT.automod)
        assert again.id == created.id
        assert again.id != automod_id
        assert bot.automod.post_card.await_count >= 1
        async with session_scope(sessions) as session:
            config = await session.get(GuildConfig, 99)
        assert config is not None
        assert config.automod_channel_id == again.id
    finally:
        await engine.dispose()


async def test_seed_reuses_english_roles_without_rename(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    await _plant_skeleton(
        guild,
        names={
            "role_special": "Special",
            "role_chief": "Chief",
            "role_senior": "Senior",
            "role_watcher": "Watcher",
        },
    )
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        report = await service.run(guild, actor_id=7)
        assert report.verdict is SeedVerdict.OK
        assert report.reused_roles == len([entry for entry in skeleton_entries() if entry.kind is SkeletonKind.ROLE])
        names = {role.name for role in guild.roles}
        assert {"Special", "Chief", "Senior", "Watcher"} <= names
        assert LAYOUT.role_special not in names
    finally:
        await engine.dispose()


async def test_seed_grants_guest_and_reliable_not_foreign_mute(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    extra = _FakeChannel("болталка")
    guild.text_channels.append(extra)
    human = _FakeMember(11)
    bot_member = _FakeMember(12, bot=True)
    guild.members = [human, bot_member]
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        await _plant_skeleton(guild)
        report = await service.run(guild, actor_id=7)
        assert report.verdict is SeedVerdict.OK
        mute = next(role for role in guild.roles if role.name == LAYOUT.role_mute)
        assert mute.id not in extra.overwrites
        guest = next(role for role in guild.roles if role.name == LAYOUT.role_guest)
        reliable = next(role for role in guild.roles if role.name == LAYOUT.role_tiers[3])
        claims = [call.args[0] for call in bot.roles.apply.await_args_list]
        activity = [claim for claim in claims if claim.subsystem is RoleSubsystem.ACTIVITY]
        reputation = [claim for claim in claims if claim.subsystem is RoleSubsystem.REPUTATION]
        assert {claim.user_id for claim in activity} == {11}
        assert all(int(guest.id) in claim.add for claim in activity)
        assert {claim.user_id for claim in reputation} == {11}
        assert all(int(reliable.id) in claim.add for claim in reputation)
    finally:
        await engine.dispose()


async def test_seed_partial_when_panel_fails(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    bot.tickets.post_panel = AsyncMock(return_value=False)
    bot.tickets.panel_is_live = AsyncMock(return_value=False)
    service = GuildSeed(bot)
    events: list[tuple[str, bool]] = []

    async def on_progress(name: str, ok: bool) -> None:
        events.append((name, ok))

    try:
        await _plant_skeleton(guild)
        report = await service.run(guild, actor_id=7, on_progress=on_progress)
        assert report.verdict is SeedVerdict.PARTIAL
        assert any("тикеты" in note for note in report.notes)
        assert ("панели", False) in events
    finally:
        await engine.dispose()


async def test_seed_never_creates_discord_objects(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    guild.create_role = AsyncMock(side_effect=AssertionError("role create must not run"))
    guild.create_text_channel = AsyncMock(side_effect=AssertionError("channel create must not run"))
    guild.create_category = AsyncMock(side_effect=AssertionError("category create must not run"))
    guild.create_voice_channel = AsyncMock(side_effect=AssertionError("voice create must not run"))
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        report = await service.run(guild, actor_id=7, mode=SeedMode.ADOPT)
        assert report.verdict is SeedVerdict.PARTIAL
        assert report.created == 0
        assert report.created_roles == 0
        assert report.created_channels == 0
        assert report.missing
        assert any("вручную" in note for note in report.notes)
        guild.create_role.assert_not_called()
        guild.create_text_channel.assert_not_called()
        guild.create_category.assert_not_called()
        guild.create_voice_channel.assert_not_called()
    finally:
        await engine.dispose()


def test_seed_wait_and_rate_limit_note() -> None:
    assert copy.seed_wait(20581.78) == "около 6 ч"
    assert copy.seed_wait(90) == "около 2 мин"
    assert copy.seed_wait(10) == "около минуты"
    note = copy.seed_rate_limited("role", 20581.78)
    assert "около 6 ч" in note
    assert "пачками" in note
    assert "не чистите базу" in copy.seed_rate_limited("channel", 81422.4)
    assert "каналы" in copy.seed_rate_limited("channel", 81422.4)


async def test_seed_ingests_created_role(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    mute = next(entry for entry in skeleton_entries() if entry.key == "role_mute")
    try:
        assert await service.ingest(guild, kind=SkeletonKind.ROLE, name="нет такой", actor_id=7) is None
        await guild.create_role(name=mute.name, colour=SimpleNamespace(value=mute.colour), hoist=mute.hoist)
        report = await service.ingest(guild, kind=SkeletonKind.ROLE, name=mute.name, actor_id=7)
        assert report is not None
        assert report.verdict in {SeedVerdict.OK, SeedVerdict.PARTIAL}
        assert "role_mute" in report.by_name or "role_mute" in report.bound
        async with session_scope(sessions) as session:
            settings = await session.get(ModSettings, 99)
        assert settings is not None
        assert settings.mute_role_id
        again = await service.ingest(guild, kind=SkeletonKind.ROLE, name=mute.name, actor_id=7)
        assert again is not None
        assert again.verdict is SeedVerdict.OK
        assert again.by_name == ()
    finally:
        await engine.dispose()


async def test_mutation_guard_respects_discord_hold_without_probe() -> None:
    now = 1000.0
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=None))
    remember(bot, 99, BUCKET_CHANNEL, 80.0, now=now, source=HoldSource.DISCORD)
    hold = await before_mutate(bot, 99, BUCKET_CHANNEL, now=now + 1.0)
    assert hold is not None
    assert hold.bucket == BUCKET_CHANNEL


async def test_seed_reuses_discord_objects_after_empty_database(tmp_path: Path) -> None:
    first_engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'one.db').as_posix()}")
    await apply_migrations(first_engine)
    guild = _FakeGuild(99)
    await _plant_skeleton(guild)
    first = GuildSeed(_bot(create_sessionmaker(first_engine), guild))
    try:
        built = await first.run(guild, actor_id=7)
        assert built.verdict is SeedVerdict.OK
        assert built.created_roles == 0
        assert built.created_channels == 0
        assert built.reused_roles > 0
        assert built.reused_channels > 0
    finally:
        await first_engine.dispose()

    second_engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'two.db').as_posix()}")
    await apply_migrations(second_engine)
    second = GuildSeed(_bot(create_sessionmaker(second_engine), guild))
    try:
        again = await second.run(guild, actor_id=7)
        assert again.created_roles == 0
        assert again.created_channels == 0
        assert again.reused_roles == built.reused_roles
        assert again.reused_channels == built.reused_channels
    finally:
        await second_engine.dispose()


async def test_schedule_ingest_does_not_cancel_running_flush() -> None:
    mute = next(entry for entry in skeleton_entries() if entry.key == "role_mute")
    guild = SimpleNamespace(id=99)
    service = GuildSeed(SimpleNamespace(serves=lambda _gid: True))
    running = asyncio.create_task(asyncio.sleep(30))
    service._flush[99] = running
    service.schedule_ingest(guild, SkeletonKind.ROLE, mute.name)  # type: ignore[arg-type]
    assert not running.cancelled()
    assert "role_mute" in service._pending[99]
    running.cancel()
    with suppress(asyncio.CancelledError):
        await running


async def test_cancel_pending_skips_busy_flush() -> None:
    service = GuildSeed(SimpleNamespace())
    running = asyncio.create_task(asyncio.sleep(30))
    service._flush[99] = running
    service._busy.add(99)
    service._pending[99] = {"verify"}
    service.cancel_pending(99)
    assert not running.cancelled()
    running.cancel()
    with suppress(asyncio.CancelledError):
        await running


async def test_flush_ingest_swallows_execute_error_and_drains_later_keys() -> None:
    guild = SimpleNamespace(id=99)
    service = GuildSeed(SimpleNamespace(serves=lambda _gid: True, user=SimpleNamespace(id=1)))
    service.bot_can_seed = lambda _guild: True  # type: ignore[method-assign]
    seen: list[set[str]] = []

    async def boom(*_args: object, **kwargs: object) -> SeedReport:
        keys = set(kwargs.get("only_keys") or ())
        seen.append(keys)
        if "role_mute" in keys:
            service._pending.setdefault(99, set()).add("verify")
            raise RuntimeError("disk")
        return SeedReport(verdict=SeedVerdict.OK)

    service._execute = boom  # type: ignore[method-assign]
    service._pending[99] = {"role_mute"}
    with patch("conductor.services.seed.INGEST_DEBOUNCE", 0):
        await service._flush_ingest(guild)  # type: ignore[arg-type]
        extra = service._flush.get(99)
        if extra is not None:
            await extra
    assert {"role_mute"} in seen
    assert any("verify" in keys for keys in seen)


async def test_seed_run_returns_failed_when_execute_raises(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    service._execute = AsyncMock(side_effect=RuntimeError("disk"))  # type: ignore[method-assign]
    try:
        report = await service.run(guild, actor_id=7)
        assert report.verdict is SeedVerdict.FAILED
        assert copy.SEED_EXECUTE_FAILED in report.notes
    finally:
        await engine.dispose()
