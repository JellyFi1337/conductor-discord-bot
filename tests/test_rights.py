from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import discord

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.domain.rights import MEMBER_TARGETS, TARGETS, missing_rules, rule_flags, rules_for
from conductor.domain.seed import LAYOUT, SKELETON, ExistingItem, SkeletonKind, entry_by_key, skeleton_entries
from conductor.presentation import copy
from conductor.services.seed import GuildSeed, _actual_rights, _overwrites_match, _right_actors
from test_seed import _bot, _FakeChannel, _FakeGuild, _plant_skeleton, _url


def _complete(entry, *, drop: tuple[str, str] | None = None) -> dict[str, dict[str, bool | None]]:
    actual: dict[str, dict[str, bool | None]] = {}
    for rule in rules_for(entry):
        flags = dict(rule_flags(rule))
        if drop is not None and rule.target == drop[0]:
            flags.pop(drop[1], None)
        actual[rule.target] = flags
    return actual


def _bound_objects(guild: _FakeGuild) -> dict[str, object]:
    objects: dict[str, object] = {}
    for entry in skeleton_entries():
        if entry.kind is SkeletonKind.ROLE:
            pool = guild.roles
        elif entry.kind is SkeletonKind.CATEGORY:
            pool = guild.categories
        elif entry.kind is SkeletonKind.VOICE:
            pool = guild.voice_channels
        else:
            pool = guild.text_channels
        objects[entry.key] = next(item for item in pool if item.name == entry.name)
    return objects


def test_every_channel_row_has_known_targets() -> None:
    for entry in SKELETON:
        rules = rules_for(entry)
        if entry.kind is SkeletonKind.ROLE:
            assert rules == ()
            continue
        assert rules
        assert {rule.target for rule in rules} <= TARGETS


def test_missing_rules_empty_when_complete_and_one_when_stripped() -> None:
    for entry in skeleton_entries():
        if entry.kind is SkeletonKind.ROLE:
            continue
        assert missing_rules(entry, _complete(entry)) == ()
        rules = rules_for(entry)
        target = rules[0].target
        flag = next(iter(rule_flags(rules[0])))
        missing = missing_rules(entry, _complete(entry, drop=(target, flag)))
        assert len(missing) == 1
        assert missing[0].target == target


def test_hub_requires_verified_connect() -> None:
    hub = entry_by_key("hub")
    assert hub is not None
    verified = next(rule for rule in rules_for(hub) if rule.target == "verified")
    assert verified.view_channel is True
    assert verified.connect is True
    assert verified.speak is True
    actual = _complete(hub)
    actual["verified"] = {**actual["verified"], "connect": None}
    missing = missing_rules(hub, actual)
    assert len(missing) == 1
    assert missing[0].target == "verified"
    assert missing[0].connect is True


def test_verify_sends_and_rules_does_not() -> None:
    verify = entry_by_key("verify")
    rules = entry_by_key("rules")
    assert verify is not None and rules is not None
    everyone_verify = next(rule for rule in rules_for(verify) if rule.target == "everyone")
    everyone_rules = next(rule for rule in rules_for(rules) if rule.target == "everyone")
    assert everyone_verify.send_messages is True
    assert everyone_verify.add_reactions is False
    assert everyone_rules.send_messages is False


def test_members_never_get_privileged_flags() -> None:
    for entry in SKELETON:
        for rule in rules_for(entry):
            assert rule.mention_everyone is not True
            if rule.target not in MEMBER_TARGETS:
                continue
            assert rule.manage_channels is not True
            assert rule.manage_messages is not True


def test_cat_rooms_and_groups_extras() -> None:
    rooms = entry_by_key("cat_rooms")
    groups = entry_by_key("cat_groups")
    evidence = entry_by_key("cat_evidence")
    assert rooms is not None and groups is not None and evidence is not None
    verified_rooms = next(rule for rule in rules_for(rooms) if rule.target == "verified")
    bot_rooms = next(rule for rule in rules_for(rooms) if rule.target == "bot")
    verified_groups = next(rule for rule in rules_for(groups) if rule.target == "verified")
    chief = next(rule for rule in rules_for(evidence) if rule.target == "chief")
    everyone = next(rule for rule in rules_for(evidence) if rule.target == "everyone")
    archive = entry_by_key("cat_group_archive")
    assert archive is not None
    archive_chief = next(rule for rule in rules_for(archive) if rule.target == "chief")
    archive_everyone = next(rule for rule in rules_for(archive) if rule.target == "everyone")
    assert verified_rooms.connect is True and verified_rooms.speak is True
    assert bot_rooms.manage_channels is True and bot_rooms.move_members is True
    assert verified_groups.connect is True
    assert everyone.view_channel is False
    assert chief.send_messages is True and chief.manage_messages is True
    assert archive_everyone.view_channel is False
    assert archive_chief.view_channel is True and archive_chief.read_message_history is True
    assert archive_chief.send_messages is False and archive_chief.add_reactions is False


async def test_apply_then_check_is_green() -> None:
    guild = _FakeGuild(99)
    await _plant_skeleton(guild)
    objects = _bound_objects(guild)
    existing = {key: ExistingItem(exists=True, overwrites_ok=False, category_ok=True) for key in objects}
    service = GuildSeed(SimpleNamespace(outbound=None))
    notes: list[str] = []
    await service._apply_rights(guild, objects, existing, notes, force=frozenset(objects))
    actors = _right_actors(guild, objects=objects, existing=existing)
    wanted = [entry_by_key("hub"), entry_by_key("verify"), entry_by_key("rules"), entry_by_key("cat_rooms")]
    pool = [entry for entry in skeleton_entries() if entry.kind is not SkeletonKind.ROLE]
    random.seed(7)
    wanted.extend(random.sample(pool, k=8))
    ids = {key: getattr(obj, "id", None) for key, obj in objects.items()}
    for entry in wanted:
        assert entry is not None
        channel = objects[entry.key]
        assert missing_rules(entry, _actual_rights(channel, actors)) == ()
        assert _overwrites_match(guild, channel, entry, ids) is True


async def test_apply_skips_matching_and_ignores_neighbors() -> None:
    guild = _FakeGuild(99)
    await _plant_skeleton(guild)
    extra = _FakeChannel("болталка")
    guild.text_channels.append(extra)
    objects = _bound_objects(guild)
    existing = {key: ExistingItem(exists=True, overwrites_ok=False) for key in objects}
    service = GuildSeed(SimpleNamespace(outbound=None))
    notes: list[str] = []
    first = await service._apply_rights(guild, objects, existing, notes, force=frozenset({"hub", "cat_rooms"}))
    assert first > 0
    assert extra.overwrites == {}
    hub = objects["hub"]
    calls = {"n": 0}
    original = hub.set_permissions

    async def counted(target: object, overwrite: object = None, **kwargs: object) -> None:
        calls["n"] += 1
        await original(target, overwrite=overwrite, **kwargs)

    hub.set_permissions = counted
    for key in existing:
        existing[key] = ExistingItem(exists=True, overwrites_ok=True, category_ok=True)
    idle = await service._apply_rights(guild, objects, existing, notes)
    assert idle == 0
    assert calls["n"] == 0
    forced = await service._apply_rights(guild, objects, existing, notes, force=frozenset({"hub"}))
    assert forced == 0
    assert calls["n"] == 0


async def test_reapply_rights_repairs_hub_connect(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    extra = _FakeChannel("болталка")
    guild.text_channels.append(extra)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        await _plant_skeleton(guild)
        await service.run(guild, actor_id=7)
        hub = next(item for item in guild.voice_channels if item.name == LAYOUT.hub)
        verified = next(role for role in guild.roles if role.name == LAYOUT.role_verified)
        hub.overwrites[verified.id] = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            connect=False,
            speak=True,
        )
        report = await service.reapply_rights(guild, actor_id=7, keys=frozenset({"hub"}))
        assert report.repaired >= 1
        restored = hub.overwrites[verified.id]
        assert restored.connect is True
        assert extra.overwrites == {}
    finally:
        await engine.dispose()


async def test_forbidden_below_bot_has_distinct_copy() -> None:
    guild = _FakeGuild(99)
    await _plant_skeleton(guild)
    objects = _bound_objects(guild)
    existing = {key: ExistingItem(exists=True, overwrites_ok=False) for key in objects}
    hub = objects["hub"]
    verified = objects["role_verified"]
    verified.position = guild.bot_role.position + 5

    async def boom(target: object, overwrite: object = None, **_kwargs: object) -> None:
        del overwrite
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing permissions")

    hub.set_permissions = boom
    service = GuildSeed(SimpleNamespace(outbound=None))
    notes: list[str] = []
    await service._apply_rights(guild, objects, existing, notes, force=frozenset({"hub"}))
    assert copy.rights_bot_below(hub, verified) in notes
