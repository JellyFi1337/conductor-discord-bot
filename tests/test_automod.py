from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import AutomodHit, AutomodRequest, MemberRecord, Panel
from conductor.db.session import session_scope
from conductor.domain.access import AUTOMOD_REQUEST, AccessSnapshot, Rank, can, empty_rank_roles
from conductor.domain.automod import (
    DEFAULT_MAPPING,
    EDIT_RULES,
    AutomodAction,
    AutomodConfig,
    ConfigError,
    DeleteMode,
    ExemptCause,
    MessageFacts,
    Newcomer,
    RequestStatus,
    RollingWindow,
    Trigger,
    after_trigger,
    card_fingerprint,
    count_emoji,
    count_gifs,
    default_config,
    effective_limit,
    evaluate,
    exempt_cause,
    fill_missing_rules,
    foreign_invites,
    has_everyone,
    has_invite,
    invite_codes,
    may_decide_automod,
    normalize_for_duplicate,
    parse_config,
    rule_specs,
    sanitize_request,
)
from conductor.domain.discord_limits import AUDIT_REASON
from conductor.domain.moderation import CaseKind
from conductor.presentation import copy
from conductor.presentation.automod import RequestModal, request_card, rules_card
from conductor.presentation.layout import collected_text
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.automod import AutomodService, collect_facts
from conductor.services.automod_rules import AutomodRules, example_automod_path, load
from conductor.services.cache import CacheHub

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
FAMILY = "👨‍👩‍👧"
FLAG = "🇷🇺"
CUSTOM = "<:wave:123456789012345678>"
ANIMATED = "<a:dance:123456789012345679>"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _facts(
    *,
    text: str = "",
    mid: int = 1,
    mentions: int = 0,
    roles: int = 0,
    names: tuple[str, ...] = (),
    urls: tuple[str, ...] = (),
    since: datetime | None = None,
    everyone: bool = False,
    foreign: int = 0,
    codes: tuple[str, ...] = (),
    fail_closed: bool = False,
) -> MessageFacts:
    return MessageFacts(
        guild_id=1,
        user_id=2,
        channel_id=3,
        message_id=mid,
        text=text,
        attachment_names=names,
        urls=urls,
        user_mentions=mentions,
        role_mentions=roles,
        member_since=since,
        everyone=everyone,
        foreign_invites=foreign,
        foreign_codes=codes,
        invite_fail_closed=fail_closed,
    )


def _only(*names: str, **overrides: object) -> AutomodConfig:
    mapping = deepcopy(DEFAULT_MAPPING)
    mapping.update(overrides)
    rules = mapping["rules"]
    assert isinstance(rules, dict)
    mapping["rules"] = {name: deepcopy(rules[name]) for name in names}
    parsed = parse_config(mapping)
    assert isinstance(parsed, AutomodConfig)
    return parsed


def _bot() -> SimpleNamespace:
    posted: list[tuple[int, object]] = []
    return SimpleNamespace(
        caches=CacheHub(),
        sessions=None,
        guilds=(),
        log_pump=SimpleNamespace(submit=lambda channel_id, view=None, **_kw: posted.append((channel_id, view))),
        config_by_guild={},
        serves=lambda _gid: True,
        posted=posted,
    )


def test_example_file_loads_without_errors() -> None:
    result = load(example_automod_path())
    assert result.missing is False
    assert result.errors == ()
    assert result.config is not None
    assert set(result.config.rules) == {
        "emoji_flood",
        "invite_link",
        "everyone_mention",
        "duplicate",
        "user_mentions",
        "role_mentions",
        "gif_flood",
    }
    assert result.config.window_minutes == 60
    assert result.config.timeout_minutes == 10


def test_parser_rejects_unknown_field_with_path() -> None:
    mapping = deepcopy(DEFAULT_MAPPING)
    rules = mapping["rules"]
    assert isinstance(rules, dict)
    flood = dict(rules["emoji_flood"])
    flood["limt"] = 10
    rules["emoji_flood"] = flood
    parsed = parse_config(mapping)
    assert isinstance(parsed, list)
    assert any(item.path == "rules.emoji_flood.limt" for item in parsed)


def test_parser_rejects_wrong_type() -> None:
    mapping = deepcopy(DEFAULT_MAPPING)
    mapping["window_minutes"] = "hour"
    parsed = parse_config(mapping)
    assert isinstance(parsed, list)
    assert any(item.path == "window_minutes" for item in parsed)


def test_parser_rejects_unknown_top_level() -> None:
    mapping = deepcopy(DEFAULT_MAPPING)
    mapping["window"] = 60
    parsed = parse_config(mapping)
    assert isinstance(parsed, list)
    assert any(item.path == "window" for item in parsed)


def test_count_emoji_custom_and_compound() -> None:
    assert count_emoji(f"{CUSTOM} {ANIMATED}") == 2
    assert count_emoji(FAMILY) == 1
    assert count_emoji(FLAG) == 1
    assert count_emoji("😀" * 3) == 3
    assert count_emoji("hello") == 0


def test_has_invite_known_hosts_only() -> None:
    assert has_invite("join discord.gg/abc")
    assert has_invite("https://discord.com/invite/xyz")
    assert has_invite("https://discordapp.com/invite/xyz")
    assert has_invite("discord. gg/abc")
    assert not has_invite("https://example.com/invite")


def test_invite_codes_finds_hosts_and_obfuscations() -> None:
    hosts = (
        "https://discord.com/invite/one",
        "https://discordapp.com/invite/two",
        "discord.gg/three",
        "dsc.gg/four",
        "discord.me/five",
        "discord.io/six",
        "invite.gg/seven",
        "disboard.org/server/join/eight",
    )
    found = invite_codes(" ".join(hosts))
    assert found == ("one", "two", "three", "four", "five", "six", "seven", "eight")
    assert invite_codes("discord . gg/abc") == ("abc",)
    assert invite_codes("discord[.]gg/abc") == ("abc",)
    assert invite_codes("discord(dot)gg/abc") == ("abc",)


def test_has_everyone_normalizes_and_skips_code() -> None:
    assert has_everyone("@everyone")
    assert has_everyone("hi @here")
    assert has_everyone("@\u200beveryone")
    assert not has_everyone("`@everyone`")
    assert not has_everyone("mail@everyone.com")
    assert has_everyone("＠everyone")
    assert not has_everyone("@Here we go")


def test_normalize_for_duplicate_folds_punct_and_spaces() -> None:
    assert normalize_for_duplicate("Hello,  WORLD!!") == "hello world"
    assert normalize_for_duplicate("hello world") == normalize_for_duplicate("HELLO   world.")


def test_count_gifs_attachments_and_links() -> None:
    assert count_gifs(("cat.gif", "note.txt"), ()) == 1
    assert count_gifs((), ("https://tenor.com/view/x",)) == 1
    assert count_gifs((), ("https://media.giphy.com/media/x/giphy.gif",)) == 1
    assert count_gifs((), ("https://media.discordapp.net/attachments/1/2/x.gif",)) == 1
    assert count_gifs(("note.png",), ("https://example.com/a.png",)) == 0


def test_each_rule_is_silent_at_n_and_fires_at_n_plus_one() -> None:
    cases = (
        ("emoji_flood", 10, lambda n, mid: _facts(text="😀" * n, mid=mid)),
        ("user_mentions", 5, lambda n, mid: _facts(mentions=n, mid=mid)),
        ("role_mentions", 3, lambda n, mid: _facts(roles=n, mid=mid)),
        ("gif_flood", 5, lambda n, mid: _facts(names=("a.gif",) * n, mid=mid)),
    )
    for name, limit, make in cases:
        config = _only(name)
        state = RollingWindow()
        silent = evaluate(make(limit, 1), state, config, NOW)
        assert silent == [], name
        fired = evaluate(make(1, 2), state, config, NOW)
        assert [item.rule for item in fired] == [name], name
        assert fired[0].used == limit + 1
        assert fired[0].action is AutomodAction.TIMEOUT


def test_invite_link_fires_on_the_first() -> None:
    config = _only("invite_link")
    state = RollingWindow()
    fired = evaluate(_facts(text="https://discord.gg/room", foreign=1), state, config, NOW)
    assert [item.rule for item in fired] == ["invite_link"]
    assert fired[0].used == 1
    assert fired[0].delete is DeleteMode.MESSAGE


def test_everyone_mention_fires_on_the_first() -> None:
    config = _only("everyone_mention")
    state = RollingWindow()
    fired = evaluate(_facts(text="@everyone", everyone=True), state, config, NOW)
    assert [item.rule for item in fired] == ["everyone_mention"]
    assert fired[0].used == 1
    assert fired[0].timeout_minutes == 60


def test_counts_reset_after_the_window() -> None:
    config = _only("emoji_flood")
    state = RollingWindow()
    assert evaluate(_facts(text="😀" * 10), state, config, NOW) == []
    later = NOW + timedelta(minutes=61)
    assert evaluate(_facts(text="😀", mid=2), state, config, later) == []


def test_newcomer_fires_on_ceil_limit_times_factor_plus_one() -> None:
    config = _only("emoji_flood")
    rule = config.rules["emoji_flood"]
    since = NOW - timedelta(hours=12)
    limit = effective_limit(rule, True, config.newcomer.factor)
    assert limit == 5
    state = RollingWindow()
    assert evaluate(_facts(text="😀" * limit, since=since), state, config, NOW) == []
    fired = evaluate(_facts(text="😀", mid=2, since=since), state, config, NOW)
    assert [item.rule for item in fired] == ["emoji_flood"]
    assert fired[0].limit == 5
    assert fired[0].used == 6


def test_factor_one_disables_newcomer() -> None:
    config = _only("emoji_flood", newcomer={"hours": 24, "factor": 1})
    since = NOW - timedelta(hours=1)
    state = RollingWindow()
    assert evaluate(_facts(text="😀" * 10, since=since), state, config, NOW) == []
    fired = evaluate(_facts(text="😀", mid=2, since=since), state, config, NOW)
    assert fired[0].limit == 10


def test_newcomer_hours_zero_means_zero_days() -> None:
    config = _only("emoji_flood", newcomer={"hours": 0, "factor": 0.5})
    assert config.newcomer.days == 0
    since = NOW - timedelta(minutes=1)
    state = RollingWindow()
    assert evaluate(_facts(text="😀" * 10, since=since), state, config, NOW) == []
    fired = evaluate(_facts(text="😀", mid=2, since=since), state, config, NOW)
    assert fired[0].limit == 10


def test_duplicates_use_normalized_text() -> None:
    config = _only("duplicate")
    state = RollingWindow()
    assert evaluate(_facts(text="Hello, WORLD"), state, config, NOW) == []
    assert evaluate(_facts(text="hello   world!", mid=2), state, config, NOW) == []
    assert evaluate(_facts(text="HELLO WORLD.", mid=3), state, config, NOW) == []
    fired = evaluate(_facts(text="hello world", mid=4), state, config, NOW)
    assert [item.rule for item in fired] == ["duplicate"]
    assert state.message_ids("duplicate", text="hello world") == (1, 2, 3, 4)


def test_after_trigger_resets_the_rule_counter() -> None:
    config = _only("emoji_flood")
    state = RollingWindow()
    fired = evaluate(_facts(text="😀" * 11), state, config, NOW)
    assert fired
    after_trigger(state, "emoji_flood")
    assert evaluate(_facts(text="😀" * 10, mid=2), state, config, NOW) == []
    again = evaluate(_facts(text="😀", mid=3), state, config, NOW)
    assert [item.rule for item in again] == ["emoji_flood"]


def test_missing_file_uses_built_in_defaults(tmp_path: Path) -> None:
    service = AutomodRules(_bot(), tmp_path / "automod.yml")
    result = service.load()
    assert result.missing is True
    assert service.file_missing is True
    assert service.config.window_minutes == default_config().window_minutes
    assert set(service.config.rules) == set(default_config().rules)


def test_bad_file_keeps_previous_config(tmp_path: Path) -> None:
    path = tmp_path / "automod.yml"
    text = example_automod_path().read_text(encoding="utf-8")
    path.write_text(text.replace("window_minutes: 60", "window_minutes: 30"), encoding="utf-8")
    service = AutomodRules(_bot(), path)
    ok = service.load()
    assert ok.errors == ()
    assert service.config.window_minutes == 30
    path.write_text("window_minutes: 15\nunknown: true\n", encoding="utf-8")
    bad = service.load()
    assert bad.errors
    assert any(item.path == "unknown" for item in bad.errors)
    assert service.config.window_minutes == 30


def test_rejected_file_posts_admin_card(tmp_path: Path) -> None:
    path = tmp_path / "automod.yml"
    text = example_automod_path().read_text(encoding="utf-8")
    path.write_text(text.replace("limit: 10", "limt: 10"), encoding="utf-8")
    bot = _bot()
    bot.guilds = (SimpleNamespace(id=7),)
    bot.config_by_guild = {7: SimpleNamespace(admin_log_channel_id=99)}
    service = AutomodRules(bot, path)
    result = service.load()
    assert result.errors
    assert bot.posted
    channel_id, view = bot.posted[0]
    assert channel_id == 99
    text = collected_text(view)
    assert copy.AUTOMOD_FILE_REJECTED_TITLE in text
    assert "файл правил не принят" in text
    assert "rules.emoji_flood.limt" in text


async def test_record_hit_writes_row(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    sessions = create_sessionmaker(engine)
    try:
        await apply_migrations(engine)
        bot = _bot()
        bot.sessions = sessions
        service = AutomodRules(bot, tmp_path / "missing.yml")
        await service.record_hit(
            guild_id=1,
            user_id=2,
            rule="emoji_flood",
            channel_id=3,
            message_id=4,
            now=NOW,
        )
        async with session_scope(sessions) as session:
            rows = list((await session.scalars(select(AutomodHit))).all())
        assert len(rows) == 1
        assert rows[0].rule == "emoji_flood"
        assert rows[0].message_id == 4
        assert rows[0].case_id is None
    finally:
        await engine.dispose()


def test_config_error_str_includes_path() -> None:
    err = ConfigError("rules.emoji_flood.limt", "unknown_field")
    assert str(err) == "rules.emoji_flood.limt: unknown_field"
    assert copy.automod_cfg_error(err) == "rules.emoji_flood.limt: неизвестное поле"


WATCHER_ROLE = 11
SENIOR_ROLE = 12
CHIEF_ROLE = 13
SPECIAL_ROLE = 14


def test_exempt_rank_and_admin_and_channel() -> None:
    config = default_config()
    assert (
        exempt_cause(
            channel_id=3,
            rank="watcher",
            role_ids=(),
            is_administrator=False,
            is_guild_owner=False,
            config=config,
        )
        is ExemptCause.RANK
    )
    assert (
        exempt_cause(
            channel_id=3,
            rank="none",
            role_ids=(),
            is_administrator=True,
            is_guild_owner=False,
            config=config,
        )
        is ExemptCause.LIMITS
    )
    assert (
        exempt_cause(
            channel_id=9,
            rank="none",
            role_ids=(),
            is_administrator=False,
            is_guild_owner=False,
            config=config,
            log_channel_ids=(9,),
        )
        is ExemptCause.CHANNEL
    )
    assert (
        exempt_cause(
            channel_id=3,
            rank="none",
            role_ids=(),
            is_administrator=False,
            is_guild_owner=False,
            config=config,
        )
        is None
    )


def test_evaluate_only_invite_skips_other_counters() -> None:
    config = default_config()
    state = RollingWindow()
    facts = _facts(text="😀" * 11 + " https://discord.gg/room", foreign=1)
    fired = evaluate(facts, state, config, NOW, only=("invite_link",))
    assert [item.rule for item in fired] == ["invite_link"]
    assert state.used("emoji_flood") == 0


class _Partial:
    def __init__(self, mid: int, deleted: list[int]) -> None:
        self.id = mid
        self._deleted = deleted

    async def delete(self) -> None:
        self._deleted.append(self.id)


class _Channel:
    def __init__(self, channel_id: int = 3) -> None:
        self.id = channel_id
        self.name = "общий-чат"
        self.deleted: list[int] = []
        self.bulk: list[list[int]] = []

    def get_partial_message(self, mid: int) -> _Partial:
        return _Partial(int(mid), self.deleted)

    async def delete_messages(self, items: list[object]) -> None:
        self.bulk.append([int(item.id) for item in items])  # type: ignore[attr-defined]


class _Moderation:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def apply(self, guild: object, actor: object, target: object, **kwargs: object) -> SimpleNamespace:
        self.calls.append({"actor": getattr(actor, "id", 0), "target": getattr(target, "id", 0), **kwargs})
        return SimpleNamespace(case=SimpleNamespace(id=50 + len(self.calls)))

    async def active_watch(self, guild_id: int, user_id: int, **_kwargs: object) -> None:
        return None


def _member(
    *,
    admin: bool = False,
    roles: tuple[int, ...] = (),
    timed_out: datetime | None = None,
    user_id: int = 2,
) -> SimpleNamespace:
    person = SimpleNamespace(
        id=user_id,
        bot=False,
        roles=[SimpleNamespace(id=role_id) for role_id in roles],
        guild_permissions=SimpleNamespace(administrator=admin),
        joined_at=NOW - timedelta(days=10),
        timed_out_until=timed_out,
        dms=[],
    )

    async def send(**kwargs: object) -> None:
        person.dms.append(kwargs)

    person.send = send
    return person


def _guild(member: SimpleNamespace, channel: _Channel, extra: object | None = None) -> SimpleNamespace:
    channels = {int(channel.id): channel}
    if extra is not None:
        channels[int(extra.id)] = extra
    return SimpleNamespace(
        id=1,
        owner_id=99,
        vanity_url_code=None,
        me=SimpleNamespace(id=10, roles=()),
        get_member=lambda uid: member if uid == member.id else None,
        get_channel=lambda cid: channels.get(int(cid)),
    )


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.dms: list[object] = []

    async def send(self, **kwargs: object) -> None:
        self.dms.append(kwargs)


class _Board:
    def __init__(self, channel_id: int = 30) -> None:
        self.id = channel_id
        self.sent: list[object] = []
        self.edits: list[object] = []
        self.messages: dict[int, SimpleNamespace] = {}

    async def send(self, **kwargs: object) -> SimpleNamespace:
        mid = 500 + len(self.sent)

        async def edit(**kw: object) -> None:
            self.edits.append(kw)

        message = SimpleNamespace(id=mid, edit=edit)
        self.sent.append(kwargs)
        self.messages[mid] = message
        return message

    async def fetch_message(self, mid: int) -> SimpleNamespace:
        return self.messages[int(mid)]


async def _runtime(tmp_path: Path, *, member: SimpleNamespace | None = None, channel_id: int = 3):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    channel = _Channel(channel_id)
    board = _Board()
    person = member or _member()
    guild = _guild(person, channel, board)
    posted: list[tuple[int, object]] = []
    staff: list[dict[str, object]] = []

    async def post(*_args: object, **kwargs: object) -> int:
        staff.append(kwargs)
        return 1

    owner = _User(99)
    author = _User(person.id)
    users = {int(owner.id): owner, int(author.id): author}

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        user=SimpleNamespace(id=10),
        settings=SimpleNamespace(owner_user_ids=(99,)),
        owner_id=99,
        owner_ids=(),
        is_developer=lambda uid: int(uid) == 99,
        config_by_guild={
            1: SimpleNamespace(
                user_log_channel_id=20,
                admin_log_channel_id=11,
                staff_feed_channel_id=21,
                automod_channel_id=30,
            ),
        },
        log_pump=SimpleNamespace(submit=lambda cid, view=None, **_k: posted.append((cid, view))),
        get_guild=lambda gid: guild if gid == 1 else None,
        serves=lambda _gid: True,
        outbound=None,
        posted=posted,
        staff=staff,
        staff_feed=SimpleNamespace(post=post),
        board=board,
        owner_user=owner,
        author_user=author,
        get_user=lambda uid: users.get(int(uid)),
        guilds=(guild,),
    )
    bot.caches.permissions.set(
        (1, SNAPSHOT_CACHE_KEY),
        AccessSnapshot(
            rank_roles=empty_rank_roles()
            | {
                Rank.WATCHER: (WATCHER_ROLE,),
                Rank.SENIOR: (SENIOR_ROLE,),
                Rank.CHIEF: (CHIEF_ROLE,),
                Rank.SPECIAL: (SPECIAL_ROLE,),
            },
            rules={},
            special_locked=True,
        ),
    )
    bot.automod_rules = AutomodRules(bot, tmp_path / "missing.yml")
    bot.automod_rules.load()
    bot.moderation = _Moderation()
    service = AutomodService(bot)
    bot.automod = service
    return bot, service, person, channel, engine


def _message(
    guild: SimpleNamespace,
    member: SimpleNamespace,
    channel: _Channel,
    *,
    text: str,
    mid: int = 1,
    webhook_id: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        guild=guild,
        author=member,
        channel=channel,
        content=text,
        webhook_id=webhook_id,
        attachments=(),
        embeds=(),
        mentions=(),
        role_mentions=(),
        mention_everyone=False,
        message_snapshots=(),
    )


async def test_eleventh_emoji_deletes_and_timeouts(tmp_path: Path) -> None:
    bot, service, person, channel, engine = await _runtime(tmp_path)
    try:
        triggers = await service.handle(_facts(text="😀" * 11), now=NOW)
        assert [item.rule for item in triggers] == ["emoji_flood"]
        assert channel.deleted == [1]
        assert len(bot.moderation.calls) == 1
        call = bot.moderation.calls[0]
        assert call["system"] is True
        assert call["kind"] is CaseKind.TIMEOUT
        assert call["duration_raw"] == "10м"
        assert "флуд эмодзи" in str(call["reason"])
        assert len(str(call["reason"])) <= AUDIT_REASON
        assert person.dms
        assert bot.posted
        text = collected_text(bot.posted[0][1])
        assert copy.AUTOMOD_HIT_TITLE in text
        async with session_scope(bot.sessions) as session:
            rows = list((await session.scalars(select(AutomodHit))).all())
        assert len(rows) == 1
        assert rows[0].rule == "emoji_flood"
        assert rows[0].case_id == 51
    finally:
        await engine.dispose()


async def test_staff_rank_is_silent(tmp_path: Path) -> None:
    member = _member(roles=(WATCHER_ROLE,))
    bot, service, person, channel, engine = await _runtime(tmp_path, member=member)
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11)
        assert await service.inspect(message) is True
        assert channel.deleted == []
        assert bot.moderation.calls == []
        assert bot.posted == []
    finally:
        await engine.dispose()


async def test_exempt_role_is_silent(tmp_path: Path) -> None:
    member = _member(roles=(99,))
    bot, service, person, channel, engine = await _runtime(tmp_path, member=member)
    try:
        bot.automod_rules.config = replace(
            bot.automod_rules.config,
            exempt=replace(bot.automod_rules.config.exempt, roles=(99,)),
        )
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11)
        assert await service.inspect(message) is True
        assert channel.deleted == []
        assert bot.moderation.calls == []
        assert bot.posted == []
    finally:
        await engine.dispose()


async def test_exempt_channel_is_never_checked(tmp_path: Path) -> None:
    bot, service, person, channel, engine = await _runtime(tmp_path, channel_id=77)
    try:
        bot.automod_rules.config = replace(
            bot.automod_rules.config,
            exempt=replace(bot.automod_rules.config.exempt, channels=(77,)),
        )
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11)
        assert await service.inspect(message) is True
        assert channel.deleted == []
        assert bot.moderation.calls == []
        assert bot.posted == []
    finally:
        await engine.dispose()


async def test_administrator_bypass_is_logged(tmp_path: Path) -> None:
    member = _member(admin=True)
    bot, service, person, channel, engine = await _runtime(tmp_path, member=member)
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11)
        assert await service.inspect(message) is True
        assert channel.deleted == []
        assert bot.moderation.calls == []
        assert bot.posted
        text = collected_text(bot.posted[0][1])
        assert copy.AUTOMOD_BYPASS in text
    finally:
        await engine.dispose()


async def test_log_channel_is_never_checked(tmp_path: Path) -> None:
    bot, service, person, channel, engine = await _runtime(tmp_path, channel_id=20)
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11)
        assert await service.inspect(message) is True
        assert channel.deleted == []
        assert bot.moderation.calls == []
        assert bot.posted == []
    finally:
        await engine.dispose()


async def test_one_timeout_per_window(tmp_path: Path) -> None:
    bot, service, _person, channel, engine = await _runtime(tmp_path)
    try:
        await service.handle(_facts(text="😀" * 11, mid=1), now=NOW)
        await service.handle(_facts(text="😀" * 11, mid=2), now=NOW)
        assert channel.deleted == [1, 2]
        assert len(bot.moderation.calls) == 1
    finally:
        await engine.dispose()


async def test_active_timeout_skips_new_case(tmp_path: Path) -> None:
    member = _member(timed_out=NOW + timedelta(minutes=5))
    bot, service, _person, channel, engine = await _runtime(tmp_path, member=member)
    try:
        await service.handle(_facts(text="😀" * 11), now=NOW)
        assert channel.deleted == [1]
        assert bot.moderation.calls == []
        async with session_scope(bot.sessions) as session:
            rows = list((await session.scalars(select(AutomodHit))).all())
        assert len(rows) == 1
        assert rows[0].case_id is None
    finally:
        await engine.dispose()


async def test_edit_with_invite_enqueues_only_invite_rule(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    captured: list[tuple[object, object]] = []
    bot.queue = SimpleNamespace(submit=lambda key, job: captured.append((key, job)))
    try:
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=3,
            message_id=8,
            cached_message=SimpleNamespace(content="hi", message_snapshots=()),
            data={"content": "https://discord.gg/room", "author": {"id": "2", "bot": False}},
        )
        assert await service.inspect_edit(payload) is True
        assert captured[0][0] == (1, 2)
        triggers = await service.handle(
            _facts(text="😀" * 11 + " https://discord.gg/room", mid=8),
            only=EDIT_RULES,
            now=NOW,
        )
        assert [item.rule for item in triggers] == ["invite_link"]
    finally:
        await engine.dispose()


def test_enqueue_uses_guild_user_key() -> None:
    captured: list[object] = []
    bot = SimpleNamespace(queue=SimpleNamespace(submit=lambda key, job: captured.append(key)), spawn=None)
    AutomodService(bot).enqueue(_facts())
    assert captured == [(1, 2)]


async def test_inspect_enqueues_and_does_not_delete(tmp_path: Path) -> None:
    bot, service, person, channel, engine = await _runtime(tmp_path)
    captured: list[tuple[object, object]] = []
    bot.queue = SimpleNamespace(submit=lambda key, job: captured.append((key, job)))
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11)
        assert await service.inspect(message) is True
        assert captured[0][0] == (1, 2)
        assert channel.deleted == []
        assert bot.moderation.calls == []
    finally:
        await engine.dispose()


async def test_webhook_message_is_ignored(tmp_path: Path) -> None:
    bot, service, person, channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11, webhook_id=55)
        assert await service.inspect(message) is False
        assert channel.deleted == []
    finally:
        await engine.dispose()


async def test_bot_author_is_ignored(tmp_path: Path) -> None:
    member = _member()
    member.bot = True
    bot, service, person, channel, engine = await _runtime(tmp_path, member=member)
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="😀" * 11)
        assert await service.inspect(message) is False
        assert channel.deleted == []
        assert bot.moderation.calls == []
    finally:
        await engine.dispose()


def _custom_ids(view: object) -> set[str]:
    found: set[str] = set()
    walk = getattr(view, "walk_children", None)
    if not callable(walk):
        return found
    for item in walk():
        custom_id = getattr(item, "custom_id", None)
        if isinstance(custom_id, str):
            found.add(custom_id)
    return found


def test_request_key_is_chief_and_special() -> None:
    assert can(Rank.CHIEF, key=AUTOMOD_REQUEST)
    assert can(Rank.SPECIAL, key=AUTOMOD_REQUEST)
    assert not can(Rank.WATCHER, key=AUTOMOD_REQUEST)
    assert not can(Rank.SENIOR, key=AUTOMOD_REQUEST)


def test_may_decide_automod_is_owner_only() -> None:
    assert may_decide_automod(is_owner=True)
    assert not may_decide_automod(is_owner=False)


def test_sanitize_request_needs_reason() -> None:
    assert sanitize_request(rule="emoji_flood", proposal="limit: 20", reason="") is None
    assert sanitize_request(rule="emoji_flood", proposal="limit: 20", reason="   ") is None
    assert sanitize_request(rule="", proposal="limit: 20", reason="мало") is None
    cleaned = sanitize_request(rule=" emoji_flood ", proposal=" limit: 20 ", reason=" слишком строго ")
    assert cleaned == ("emoji_flood", "limit: 20", "слишком строго")
    assert RequestModal(1).reason.component.required is False


def test_rule_sentences_match_default_file() -> None:
    sentences = [copy.automod_rule_sentence(*item) for item in rule_specs(default_config())]
    assert sentences == [
        "больше 10 эмодзи за 60 минут → тайм-аут 10 мин, сообщение удаляется",
        "1 приглашение на чужой сервер за 60 минут → тайм-аут 30 мин, сообщение удаляется",
        "1 упоминание @everyone за 60 минут → тайм-аут 60 мин, сообщение удаляется",
        "больше 3 повторов за 60 минут → тайм-аут 10 мин, повторы удаляются",
        "больше 5 упоминаний участников за 60 минут → тайм-аут 10 мин",
        "больше 3 упоминаний ролей за 60 минут → тайм-аут 10 мин",
        "больше 5 гифок за 60 минут → тайм-аут 10 мин",
    ]
    config = default_config()
    text = collected_text(
        rules_card(
            lines=sentences,
            exempt=copy.automod_exempt_line(
                config.exempt.ranks,
                administrators=config.exempt.administrators,
            ),
            hits=copy.automod_hits_line({}),
            file_path="data/automod.yml",
            changed_at=NOW,
            loaded_at=NOW,
            ref=7,
        )
    )
    assert copy.AUTOMOD_CARD_TITLE in text
    assert sentences[0] in text
    assert "файл: `data/automod.yml`" in text
    assert "срабатывания за 7 дней" in text
    assert _custom_ids(
        rules_card(
            lines=sentences,
            exempt="исключения: нет",
            hits="срабатывания за 7 дней: —",
            file_path="data/automod.yml",
            changed_at=NOW,
            loaded_at=NOW,
            ref=7,
        )
    ) == {"c:am:reload:7", "c:am:request:7"}


def test_card_fingerprint_ignores_wall_clock() -> None:
    same = dict(lines=("a",), exempt="e", hits=(("emoji_flood", 0),), mtime=1, loaded_at=2)
    assert card_fingerprint(**same) == card_fingerprint(**same)
    assert card_fingerprint(**same) != card_fingerprint(**{**same, "loaded_at": 3})


def test_request_card_hides_owner_buttons_after_decision() -> None:
    pending = request_card(
        request_id=4,
        author_id=2,
        rule="emoji_flood",
        proposal="limit: 20",
        reason="мало",
        status=RequestStatus.NEW.value,
    )
    assert _custom_ids(pending) == {"c:am:acc:4", "c:am:rej:4"}
    copy_in_channel = request_card(
        request_id=4,
        author_id=2,
        rule="emoji_flood",
        proposal="limit: 20",
        reason="мало",
        status=RequestStatus.NEW.value,
        buttons=False,
    )
    assert _custom_ids(copy_in_channel) == set()
    decided = request_card(
        request_id=4,
        author_id=2,
        rule="emoji_flood",
        proposal="limit: 20",
        reason="мало",
        status=RequestStatus.ACCEPTED.value,
    )
    assert _custom_ids(decided) == set()
    assert copy.AUTOMOD_STATUS_ACCEPTED in collected_text(decided)


async def test_automod_channel_is_never_checked(tmp_path: Path) -> None:
    bot, service, person, _channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, bot.board, text="😀" * 11)
        assert await service.inspect(message) is True
        assert bot.moderation.calls == []
        assert bot.posted == []
    finally:
        await engine.dispose()


async def test_rules_card_repost_edits_same_channel(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        assert await service.card_status(1) == "stale"
        assert await service.post_card(guild, bot.board, actor_id=99)
        assert len(bot.board.sent) == 1
        assert await service.card_status(1) == "live"
        async with session_scope(bot.sessions) as session:
            row = (await session.scalars(select(Panel))).one()
        assert f"c:am:reload:{row.id}" in _custom_ids(bot.board.sent[0]["view"])
        assert await service.post_card(guild, bot.board, actor_id=99)
        assert len(bot.board.sent) == 1
        assert bot.board.edits
    finally:
        await engine.dispose()


async def test_refresh_card_skips_edit_when_hash_unchanged(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        assert await service.post_card(guild, bot.board, actor_id=99)
        assert await service.refresh_card(guild) is False
        assert bot.board.edits == []
        bot.automod_rules.loaded_at = bot.automod_rules.loaded_at - timedelta(hours=1)
        assert await service.refresh_card(guild) is True
        assert bot.board.edits
        n = len(bot.board.edits)
        assert await service.refresh_card(guild) is False
        assert len(bot.board.edits) == n
    finally:
        await engine.dispose()


async def test_reload_card_redraws_changed_rules(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    path = tmp_path / "automod.yml"
    text = example_automod_path().read_text(encoding="utf-8")
    try:
        guild = bot.get_guild(1)
        assert await service.post_card(guild, bot.board, actor_id=99)
        path.write_text(text.replace("limit: 10", "limit: 20"), encoding="utf-8")
        bot.automod_rules.path = path
        view = await service.reload_card(guild)
        assert copy.AUTOMOD_RELOAD_OK in collected_text(view)
        assert bot.board.edits
        assert "больше 20 эмодзи за 60 минут" in collected_text(bot.board.edits[0]["view"])
    finally:
        await engine.dispose()


async def test_request_without_reason_is_denied(tmp_path: Path) -> None:
    bot, service, person, _channel, engine = await _runtime(tmp_path)
    try:
        view = await service.submit_request(
            1,
            author_id=person.id,
            rule="emoji_flood",
            proposal="limit: 20",
            reason="",
        )
        assert copy.AUTOMOD_REQUEST_NEED_REASON in collected_text(view)
        async with session_scope(bot.sessions) as session:
            assert list((await session.scalars(select(AutomodRequest))).all()) == []
        assert bot.owner_user.dms == []
        assert bot.board.sent == []
    finally:
        await engine.dispose()


async def test_request_reaches_owner_and_channel_copy(tmp_path: Path) -> None:
    bot, service, person, _channel, engine = await _runtime(tmp_path)
    try:
        view = await service.submit_request(
            1,
            author_id=person.id,
            rule="emoji_flood",
            proposal="limit: 20",
            reason="одиннадцатый эмодзи слишком рано",
        )
        assert copy.AUTOMOD_REQUEST_SENT in collected_text(view)
        async with session_scope(bot.sessions) as session:
            row = (await session.scalars(select(AutomodRequest))).one()
            assert row.status == RequestStatus.NEW.value
            assert row.reason == "одиннадцатый эмодзи слишком рано"
            request_id = int(row.id)
        assert copy.AUTOMOD_STATUS_NEW in collected_text(bot.board.sent[0]["view"])
        assert _custom_ids(bot.board.sent[0]["view"]) == set()
        dm_ids = _custom_ids(bot.owner_user.dms[0]["view"])
        assert dm_ids == {f"c:am:acc:{request_id}", f"c:am:rej:{request_id}"}
    finally:
        await engine.dispose()


async def test_special_rank_cannot_decide_request(tmp_path: Path) -> None:
    bot, service, person, _channel, engine = await _runtime(tmp_path)
    try:
        await service.submit_request(
            1,
            author_id=person.id,
            rule="emoji_flood",
            proposal="limit: 20",
            reason="мало",
        )
        async with session_scope(bot.sessions) as session:
            request_id = int((await session.scalars(select(AutomodRequest))).one().id)
        view = await service.decide_request(request_id, accepted=True, actor_id=7)
        assert copy.AUTOMOD_OWNER_ONLY in collected_text(view)
        async with session_scope(bot.sessions) as session:
            row = await session.get(AutomodRequest, request_id)
            assert row is not None
            assert row.status == RequestStatus.NEW.value
            assert row.decided_by is None
        rules_path = bot.automod_rules.path
        assert not rules_path.exists()
    finally:
        await engine.dispose()


async def test_owner_accept_does_not_edit_file(tmp_path: Path) -> None:
    bot, service, person, _channel, engine = await _runtime(tmp_path)
    rules = tmp_path / "automod.yml"
    rules.write_text("window_minutes: 60\n", encoding="utf-8")
    before = rules.read_text(encoding="utf-8")
    bot.automod_rules.path = rules
    try:
        await service.submit_request(
            1,
            author_id=person.id,
            rule="emoji_flood",
            proposal="limit: 20",
            reason="мало",
        )
        async with session_scope(bot.sessions) as session:
            request_id = int((await session.scalars(select(AutomodRequest))).one().id)
        view = await service.decide_request(request_id, accepted=True, actor_id=99)
        assert copy.AUTOMOD_STATUS_ACCEPTED in collected_text(view)
        async with session_scope(bot.sessions) as session:
            row = await session.get(AutomodRequest, request_id)
            assert row is not None
            assert row.status == RequestStatus.ACCEPTED.value
            assert row.decided_by == 99
        assert bot.board.edits
        assert copy.AUTOMOD_AUTHOR_ACCEPTED in collected_text(bot.author_user.dms[0]["view"])
        assert rules.read_text(encoding="utf-8") == before
    finally:
        await engine.dispose()


async def test_tenure_is_cached_until_join_invalidation(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    first = datetime(2020, 1, 1, tzinfo=UTC)
    later = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(bot.sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=2,
                    username="ann",
                    display_name="Ann",
                    first_joined_at=first,
                    last_joined_at=first,
                )
            )
        facts = await service._with_tenure(_facts(), now=NOW)
        assert facts.member_since is not None
        assert facts.member_since.replace(tzinfo=UTC) == first
        async with session_scope(bot.sessions) as session:
            row = await session.scalar(select(MemberRecord).where(MemberRecord.user_id == 2))
            assert row is not None
            row.first_joined_at = later
        cached = await service._with_tenure(_facts(), now=NOW)
        assert cached.member_since is not None
        assert cached.member_since.replace(tzinfo=UTC) == first
        bot.caches.invalidate_joined(1, 2)
        fresh = await service._with_tenure(_facts(), now=NOW)
        assert fresh.member_since is not None
        assert fresh.member_since.replace(tzinfo=UTC) == later
    finally:
        await engine.dispose()


async def test_tenure_skipped_when_newcomer_days_are_zero(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    try:
        bot.automod_rules.config = replace(
            bot.automod_rules.config,
            newcomer=Newcomer(hours=0, factor=0.5),
        )
        bot.sessions = None
        since = NOW - timedelta(minutes=5)
        facts = await service._with_tenure(_facts(since=since), now=NOW)
        assert facts.member_since == since
    finally:
        await engine.dispose()


async def test_guard_panel_accepts_stale_row(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        assert await service.post_card(guild, bot.board, actor_id=99)
        async with session_scope(bot.sessions) as session:
            row = (await session.scalars(select(Panel))).one()
            row.state = "stale"
            panel_id = int(row.id)
        resource = await service.guard_panel(1, str(panel_id))
        assert resource is not None
        assert resource.closed is False
        async with session_scope(bot.sessions) as session:
            revived = await session.get(Panel, panel_id)
            assert revived is not None
            assert revived.state == "live"
    finally:
        await engine.dispose()


async def test_refresh_after_restart_does_not_rewrite(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        assert await service.post_card(guild, bot.board, actor_id=99)
        service._card_hash.clear()
        assert await service.refresh_card(guild) is False
        assert bot.board.edits == []
    finally:
        await engine.dispose()


async def test_tenure_skipped_when_discord_join_is_already_old(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    try:
        bot.sessions = None
        since = NOW - timedelta(days=10)
        facts = await service._with_tenure(_facts(since=since), now=NOW)
        assert facts.member_since == since
    finally:
        await engine.dispose()


async def test_staff_ranks_timeout_on_everyone_except_special(tmp_path: Path) -> None:
    for rank_role, silent in (
        (WATCHER_ROLE, False),
        (SENIOR_ROLE, False),
        (CHIEF_ROLE, False),
        (SPECIAL_ROLE, True),
    ):
        member = _member(roles=(rank_role,))
        bot, service, _person, channel, engine = await _runtime(tmp_path, member=member)
        try:
            triggers = await service.handle(_facts(text="@everyone", everyone=True), now=NOW)
            if silent:
                assert triggers == []
                assert channel.deleted == []
                assert bot.moderation.calls == []
            else:
                assert [item.rule for item in triggers] == ["everyone_mention"]
                assert channel.deleted == [1]
                assert bot.moderation.calls
        finally:
            await engine.dispose()


async def test_administrator_is_exempt_from_everyone(tmp_path: Path) -> None:
    member = _member(admin=True)
    bot, service, _person, channel, engine = await _runtime(tmp_path, member=member)
    try:
        triggers = await service.handle(_facts(text="@everyone", everyone=True), now=NOW)
        assert triggers == []
        assert channel.deleted == []
        assert bot.moderation.calls == []
    finally:
        await engine.dispose()


async def test_own_server_invite_is_allowed(tmp_path: Path) -> None:
    bot, service, _person, channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        guild.vanity_url_code = "home"
        triggers = await service.handle(_facts(text="https://discord.gg/home"), now=NOW)
        assert triggers == []
        assert channel.deleted == []
        assert bot.moderation.calls == []
    finally:
        await engine.dispose()


async def test_cached_own_invite_is_allowed(tmp_path: Path) -> None:
    bot, service, _person, channel, engine = await _runtime(tmp_path)
    try:
        bot.invites = SimpleNamespace(codes_for=lambda _gid: frozenset({"cached"}))
        triggers = await service.handle(_facts(text="https://discord.gg/cached"), now=NOW)
        assert triggers == []
        assert channel.deleted == []
    finally:
        await engine.dispose()


async def test_foreign_invite_timeouts_and_fail_closed_is_in_staff(tmp_path: Path) -> None:
    bot, service, person, channel, engine = await _runtime(tmp_path)

    async def boom(_code: str) -> None:
        raise discord.HTTPException(SimpleNamespace(status=404, reason="Not Found"), "Unknown Invite")

    bot.fetch_invite = boom
    try:
        triggers = await service.handle(_facts(text="https://discord.gg/alien"), now=NOW)
        assert [item.rule for item in triggers] == ["invite_link"]
        assert channel.deleted == [1]
        assert bot.moderation.calls
        text = collected_text(bot.posted[0][1])
        assert copy.AUTOMOD_INVITE_FAIL_CLOSED in text
        assert person.dms
    finally:
        await engine.dispose()


async def test_edit_without_text_change_is_ignored(tmp_path: Path) -> None:
    bot, service, _person, _channel, engine = await _runtime(tmp_path)
    captured: list[object] = []
    bot.queue = SimpleNamespace(submit=lambda key, job: captured.append(key))
    try:
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=3,
            message_id=9,
            cached_message=SimpleNamespace(content="same", message_snapshots=()),
            data={"content": "same", "author": {"id": "2", "bot": False}},
        )
        assert await service.inspect_edit(payload) is False
        assert captured == []
    finally:
        await engine.dispose()


async def test_edit_adding_everyone_fires(tmp_path: Path) -> None:
    bot, service, _person, channel, engine = await _runtime(tmp_path)
    try:
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=3,
            message_id=10,
            cached_message=SimpleNamespace(content="hi", message_snapshots=()),
            data={"content": "hi @everyone", "author": {"id": "2", "bot": False}},
        )
        assert await service.inspect_edit(payload) is True
        triggers = await service.handle(
            _facts(text="hi @everyone", mid=10, everyone=True),
            only=EDIT_RULES,
            now=NOW,
        )
        assert [item.rule for item in triggers] == ["everyone_mention"]
        assert channel.deleted == [10]
    finally:
        await engine.dispose()


async def test_forwarded_invite_is_checked(tmp_path: Path) -> None:
    bot, service, person, channel, engine = await _runtime(tmp_path)
    try:
        guild = bot.get_guild(1)
        message = _message(guild, person, channel, text="look")
        message.message_snapshots = (SimpleNamespace(content="https://discord.gg/alien"),)
        facts = collect_facts(message)
        assert facts is not None
        assert "discord.gg/alien" in facts.text
        triggers = await service.handle(facts, now=NOW)
        assert [item.rule for item in triggers] == ["invite_link"]
        assert channel.deleted == [1]
    finally:
        await engine.dispose()


def test_automod_reason_fits_audit_and_member_copy() -> None:
    trigger = Trigger(
        rule="everyone_mention",
        used=1,
        limit=1,
        action=AutomodAction.TIMEOUT,
        delete=DeleteMode.MESSAGE,
        timeout_minutes=60,
    )
    facts = _facts(text="@everyone ping <@123> " + ("слово " * 80), everyone=True)
    until = NOW + timedelta(minutes=60)
    reason = copy.automod_reason(
        trigger,
        facts,
        rule_title=copy.automod_rule_title("everyone_mention"),
        channel_name="общий-чат" * 40,
        until=until,
        ticket_hint=copy.AUTOMOD_TICKET_HINT,
        window_minutes=60,
    )
    assert len(reason.audit) <= AUDIT_REASON
    assert copy.automod_rule_title("everyone_mention") in reason.member.replace("\u200b", "")
    assert "#общий-чат" in reason.member
    assert "<t:" in reason.member
    assert "@everyone" not in reason.member
    assert "<@123>" not in reason.member
    assert "Отрывок:" in reason.member
    assert copy.AUTOMOD_TICKET_HINT in reason.member


def test_missing_everyone_rule_is_filled_from_defaults() -> None:
    mapping = deepcopy(DEFAULT_MAPPING)
    rules = mapping["rules"]
    assert isinstance(rules, dict)
    del rules["everyone_mention"]
    parsed = parse_config(mapping)
    assert isinstance(parsed, AutomodConfig)
    assert "everyone_mention" not in parsed.rules
    filled = fill_missing_rules(parsed)
    assert "everyone_mention" in filled.rules
    assert filled.rules["everyone_mention"].action is AutomodAction.TIMEOUT


def test_action_off_disables_everyone_rule() -> None:
    mapping = deepcopy(DEFAULT_MAPPING)
    rules = mapping["rules"]
    assert isinstance(rules, dict)
    everyone = dict(rules["everyone_mention"])
    everyone["action"] = "off"
    rules["everyone_mention"] = everyone
    parsed = parse_config(mapping)
    assert isinstance(parsed, AutomodConfig)
    fired = evaluate(_facts(text="@everyone", everyone=True), RollingWindow(), parsed, NOW)
    assert fired == []


def test_foreign_invites_filters_allowed_codes() -> None:
    assert foreign_invites(("Home", "alien"), ("home",)) == ("alien",)


def test_legacy_file_keeps_strict_invite_exempt() -> None:
    import yaml

    raw = yaml.safe_load(Path("data/automod.yml.example").read_text(encoding="utf-8"))
    raw["rules"]["invite_link"] = {"limit": 1, "action": "timeout", "delete": "message"}
    raw["rules"]["everyone_mention"] = {"limit": 1, "action": "timeout", "delete": "message"}
    config = parse_config(raw)
    assert not isinstance(config, list)
    for name in ("invite_link", "everyone_mention"):
        rule = config.rules[name]
        assert rule.exempt is not None
        assert rule.exempt.ranks == ("special",)
        assert rule.exempt.administrators is True
    assert "watcher" in config.exempt.ranks
