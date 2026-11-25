from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GuildConfig, GuildLayout, GuildRank, Panel
from conductor.db.session import session_scope
from conductor.domain.access import Rank
from conductor.domain.seed import SkeletonKind
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import format_custom_id, load_persistent_ui
from conductor.presentation.structure import StructureFixButton, structure_lost_card
from conductor.presentation.tokens import Tone
from conductor.services.access import load_snapshot, set_rank_role
from conductor.services.cache import CacheHub
from conductor.services.panels import record_panel
from conductor.services.seed import GuildSeed
from conductor.services.structure import StructureLoss, StructureWatch
from test_seed import _bot, _FakeGuild, _FakeMember, _FakeRole, _url


class FakePump:
    def __init__(self) -> None:
        self.items: list[tuple[int | None, dict[str, object]]] = []

    def submit(self, channel_id: int | None, **kwargs: object) -> None:
        self.items.append((channel_id, kwargs))


def _watch(sessions: object, guild: _FakeGuild):
    bot = _bot(sessions, guild)
    bot.log_pump = FakePump()
    bot.guilds = [guild]
    bot.scheduler = SimpleNamespace(every=lambda *_a, **_k: None)
    bot.seed = GuildSeed(bot)
    bot.structure = StructureWatch(bot)
    bot.caches = getattr(bot, "caches", None) or CacheHub()
    return bot


async def _bind_layout(sessions: object, guild_id: int, key: str, channel_id: int) -> None:
    async with session_scope(sessions) as session:
        session.add(GuildLayout(guild_id=guild_id, key=key, channel_id=channel_id))
        config = await session.get(GuildConfig, guild_id)
        if config is None:
            config = GuildConfig(guild_id=guild_id)
            session.add(config)


async def _config(sessions: object, guild_id: int, *, structure: int | None = None, admin: int | None = None) -> None:
    async with session_scope(sessions) as session:
        row = await session.get(GuildConfig, guild_id)
        if row is None:
            row = GuildConfig(guild_id=guild_id)
            session.add(row)
        row.structure_channel_id = structure
        row.admin_log_channel_id = admin


async def _live_logs(guild: _FakeGuild, sessions: object) -> tuple[int, int]:
    structure = await guild.create_text_channel("структура")
    admin = await guild.create_text_channel("лог-администрации")
    await _config(sessions, int(guild.id), structure=int(structure.id), admin=int(admin.id))
    return int(structure.id), int(admin.id)


async def test_chief_role_delete_keeps_rank_row_and_holders(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    role = _FakeRole("Главный Следящий")
    guild.roles.append(role)
    guild.members.append(_FakeMember(8, roles=[role]))
    bot = _watch(sessions, guild)
    try:
        async with session_scope(sessions) as session:
            await set_rank_role(session, 99, Rank.CHIEF, int(role.id))
        await _config(sessions, 99)
        loss = await bot.structure.on_role_gone(guild, int(role.id))
        assert loss is not None
        assert loss.rank is Rank.CHIEF
        assert loss.holders == (8,)
        async with session_scope(sessions) as session:
            row = await session.scalar(
                select(GuildRank).where(GuildRank.guild_id == 99, GuildRank.role_id == int(role.id))
            )
        assert row is not None
        assert row.rank == Rank.CHIEF.key
        card = structure_lost_card(loss)
        text = collected_text(card)
        assert copy.STRUCTURE_RANK_TITLE in text
        labels = [getattr(getattr(item, "item", None), "label", "") for item in card.walk_children()]
        assert copy.STRUCTURE_FIX_RANK in labels
    finally:
        await engine.dispose()


async def test_special_role_delete_unlocks_and_danger_card(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    role = _FakeRole("Специальный Следящий")
    guild.roles.append(role)
    bot = _watch(sessions, guild)
    try:
        async with session_scope(sessions) as session:
            locked = await set_rank_role(session, 99, Rank.SPECIAL, int(role.id))
        assert locked.special_locked is True
        guild.roles = [item for item in guild.roles if int(item.id) != int(role.id)]
        await _config(sessions, 99)
        loss = await bot.structure.on_role_gone(guild, int(role.id))
        assert loss is not None
        assert loss.rank is Rank.SPECIAL
        async with session_scope(sessions) as session:
            snapshot = await load_snapshot(session, 99)
            leftover = await session.scalar(
                select(GuildRank).where(GuildRank.guild_id == 99, GuildRank.rank == Rank.SPECIAL.key)
            )
        assert leftover is None
        assert snapshot.special_locked is False
        card = structure_lost_card(loss)
        assert copy.STRUCTURE_RANK_TITLE in collected_text(card)
        container = card.children[0]
        assert int(getattr(container, "accent_colour", 0) or 0) == Tone.DANGER.colour
    finally:
        await engine.dispose()


async def test_channel_delete_clears_own_bindings_only(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _watch(sessions, guild)
    try:
        await _bind_layout(sessions, 99, "cat_staff", 501)
        await _bind_layout(sessions, 99, "cat_entry", 502)
        async with session_scope(sessions) as session:
            config = await session.get(GuildConfig, 99)
            assert config is not None
            config.automod_channel_id = 601
            config.structure_channel_id = 40
            config.admin_log_channel_id = 41
        loss = await bot.structure.on_channel_gone(guild, 601)
        assert loss is not None
        assert loss.key == "automod"
        async with session_scope(sessions) as session:
            config = await session.get(GuildConfig, 99)
            staff = await session.scalar(
                select(GuildLayout).where(GuildLayout.guild_id == 99, GuildLayout.key == "cat_staff")
            )
            entry = await session.scalar(
                select(GuildLayout).where(GuildLayout.guild_id == 99, GuildLayout.key == "cat_entry")
            )
        assert config is not None
        assert config.automod_channel_id is None
        assert config.structure_channel_id == 40
        assert staff is not None and int(staff.channel_id) == 501
        assert entry is not None and int(entry.channel_id) == 502
    finally:
        await engine.dispose()


async def test_channel_delete_marks_live_panel_stale(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _watch(sessions, guild)
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="tk",
                guild_id=99,
                channel_id=20,
                message_id=31,
                ref=1,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )
        loss = await bot.structure.on_channel_gone(guild, 20)
        assert loss is not None
        assert loss.panels_stale == 1
        async with session_scope(sessions) as session:
            row = (await session.scalars(select(Panel))).one()
        assert row.state == "stale"
    finally:
        await engine.dispose()


async def test_unbound_channel_delete_is_silent(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _watch(sessions, guild)
    try:
        await _config(sessions, 99, admin=77)
        loss = await bot.structure.on_channel_gone(guild, 9090)
        assert loss is None
        await bot.structure.notify(
            guild,
            StructureLoss(
                key="automod",
                label="#автомод",
                kind=SkeletonKind.TEXT,
                object_id=9090,
                feature="staff",
                bindings_cleared=(),
                panels_stale=0,
            ),
        )
        assert len(bot.log_pump.items) == 1
        await bot.structure.notify(
            guild,
            StructureLoss(
                key="automod",
                label="#автомод",
                kind=SkeletonKind.TEXT,
                object_id=9090,
                feature="staff",
                bindings_cleared=(),
                panels_stale=0,
            ),
        )
        assert len(bot.log_pump.items) == 1
    finally:
        await engine.dispose()


async def test_sweep_clears_missing_binding_like_event(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _watch(sessions, guild)
    try:
        await _bind_layout(sessions, 99, "cat_staff", 777)
        await _live_logs(guild, sessions)
        found = await bot.structure.sweep(guild)
        assert len(found) == 1
        assert found[0].key == "cat_staff"
        async with session_scope(sessions) as session:
            row = await session.scalar(
                select(GuildLayout).where(GuildLayout.guild_id == 99, GuildLayout.key == "cat_staff")
            )
        assert row is None
        assert bot.log_pump.items
        again = await bot.structure.sweep(guild)
        assert again == ()
        assert len(bot.log_pump.items) == 1
    finally:
        await engine.dispose()


async def test_notify_falls_back_to_admin_log(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _watch(sessions, guild)
    try:
        await _config(sessions, 99, structure=None, admin=77)
        await bot.structure.notify(
            guild,
            StructureLoss(
                key="rules",
                label="#правила",
                kind=SkeletonKind.TEXT,
                object_id=1,
                feature="entry",
                bindings_cleared=("verification_settings.rules_channel_id",),
                panels_stale=0,
            ),
        )
        assert bot.log_pump.items[0][0] == 77
    finally:
        await engine.dispose()


async def test_self_deleted_channel_is_skipped(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _watch(sessions, guild)
    try:
        await _bind_layout(sessions, 99, "cat_staff", 501)
        bot.structure.mark_self_deleted(99, 501)
        loss = await bot.structure.on_channel_gone(guild, 501)
        assert loss is None
        async with session_scope(sessions) as session:
            row = await session.scalar(
                select(GuildLayout).where(GuildLayout.guild_id == 99, GuildLayout.key == "cat_staff")
            )
        assert row is not None
    finally:
        await engine.dispose()


async def test_sweep_caps_cards_and_fix_button_custom_id(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _watch(sessions, guild)
    try:
        for key, channel_id in (("cat_entry", 1), ("cat_server", 2), ("cat_staff", 3), ("cat_tickets", 4)):
            await _bind_layout(sessions, 99, key, channel_id)
        await _live_logs(guild, sessions)
        found = await bot.structure.sweep(guild)
        assert len(found) == 4
        assert len(bot.log_pump.items) == 4
        assert copy.structure_more(1) in collected_text(bot.log_pump.items[-1][1]["view"])
        button = StructureFixButton("cat_evidence")
        assert button.kind == "sk"
        assert button.item.custom_id == format_custom_id("sk", "fix", "cat_evidence")
        assert StructureFixButton in load_persistent_ui()
    finally:
        await engine.dispose()
