"""Правила временных голосовых комнат. Без Discord и без БД."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from conductor.domain.create_lock import CreateHold
from conductor.domain.discord_limits import CATEGORY_CHILDREN, GUILD_CHANNELS

CREATE_DEBOUNCE = 2.0
EMPTY_DELETE_DELAY = 40.0
CHANNEL_NAME_LIMIT = 100
VOICE_LIMIT_MAX = 99
BITRATE_MIN = 8
BITRATE_MAX = 384
STATUS_LIMIT = 500
CLAIM_AFTER = timedelta(minutes=2)
LFG_TTL = timedelta(minutes=10)
LFG_MAX = 99
NAME_TEMPLATE_DEFAULT = "{username}"
_PRESET_SEQUENTIAL = "sequential"
_N_CHUNK = re.compile(r"(?:[\s·.\-–—/|:]+)?\{n\}(?:[\s·.\-–—/|:]+)?")
OWNER_OVERWRITE = (
    "view_channel",
    "connect",
    "speak",
    "stream",
    "send_messages",
    "read_message_history",
    "manage_messages",
    "manage_channels",
    "move_members",
    "mute_members",
    "deafen_members",
    "priority_speaker",
)

_INVITE_RE = re.compile(
    r"(?:discord(?:app)?\.gg|discord(?:app)?\.com/invite|discord\.me)/[^\s/]+",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_AD_RE = re.compile(r"(?:\bnitro\b|\bреклам)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StoredRoom:
    guild_id: int
    channel_id: int
    owner_id: int
    hub_id: int
    id: int = 0
    locked: bool = False
    owner_absent_since: datetime | None = None
    ghosted: bool = False
    code: str | None = None
    code_expires_at: datetime | None = None
    text_channel_id: int | None = None
    lfg_need: int | None = None
    lfg_expires_at: datetime | None = None


class HubPreset(StrEnum):
    NAME = "name"
    CLONE = "clone"


@dataclass(frozen=True, slots=True)
class HubSettings:
    prefix: str = ""
    template: str = NAME_TEMPLATE_DEFAULT
    preset: str = ""
    template_channel_id: int | None = None
    extra_category_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class NameCheck:
    ok: bool
    name: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RoomReconcilePlan:
    delete: tuple[int, ...]
    drop: tuple[int, ...]
    keep: tuple[int, ...]


class HubJoin(StrEnum):
    IGNORE = "ignore"
    MOVE = "move"
    CREATE = "create"


class RoomDenial(StrEnum):
    NO_HUB = "no_hub"
    HUB_GONE = "hub_gone"
    NO_CATEGORY = "no_category"
    BOT_PERMS = "bot_perms"
    HELD = "held"
    RATE = "rate"
    LIMIT = "limit"
    FAILED = "failed"


CONFIG_DENIALS = frozenset(
    {RoomDenial.NO_HUB, RoomDenial.HUB_GONE, RoomDenial.NO_CATEGORY, RoomDenial.BOT_PERMS}
)
CATEGORY_CHANNEL_CAP = CATEGORY_CHILDREN
GUILD_CHANNEL_CAP = GUILD_CHANNELS
CATEGORY_HEAL_INTERVAL = timedelta(minutes=1)
DENIAL_NOTIFY_INTERVAL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class RoomOutcome:
    channel_id: int | None = None
    denial: RoomDenial | None = None
    hold: CreateHold | None = None

    @property
    def ok(self) -> bool:
        return self.denial is None and self.channel_id is not None


def created_channel_id(result: object) -> int | None:
    channel_id = getattr(result, "channel_id", None)
    if getattr(result, "ok", None) is True and channel_id is not None:
        return int(channel_id)
    if isinstance(result, int):
        return result
    return None


def room_channel_name(display_name: str, *, limit: int = CHANNEL_NAME_LIMIT) -> str:
    text = " ".join(display_name.split()) or "комната"
    if len(text) <= limit:
        return text
    return text[: max(1, limit)]


def empty_job_key(guild_id: int, channel_id: int) -> tuple[str, int, int]:
    return ("voice-empty", guild_id, channel_id)


def create_job_key(guild_id: int, user_id: int) -> tuple[str, int, int]:
    return ("voice-create", guild_id, user_id)


def lfg_job_key(guild_id: int, room_id: int) -> tuple[str, int, int]:
    return ("voice-lfg", guild_id, room_id)


def room_for_owner(rooms: Iterable[StoredRoom], owner_id: int) -> StoredRoom | None:
    for room in rooms:
        if room.owner_id == owner_id:
            return room
    return None


def owner_overwrite() -> dict[str, bool]:
    return {name: True for name in OWNER_OVERWRITE}


def strip_seq_placeholder(template: str) -> str:
    """Вырезает {n} вместе с соседним разделителем. Старые шаблоны нумерации."""
    cleaned = _N_CHUNK.sub(" ", template)
    return " ".join(cleaned.split()) or NAME_TEMPLATE_DEFAULT


def template_has_numbering(template: str) -> bool:
    return "{n}" in template


def hub_join_action(*, in_hub: bool, is_bot: bool, existing: StoredRoom | None) -> HubJoin:
    if is_bot or not in_hub:
        return HubJoin.IGNORE
    if existing is not None:
        return HubJoin.MOVE
    return HubJoin.CREATE


def reconcile_rooms(
    rooms: Iterable[StoredRoom],
    *,
    hub_ids: Iterable[int],
    existing_channel_ids: Iterable[int],
    occupied: Mapping[int, int],
) -> RoomReconcilePlan:
    """Пустые снести, пропавшие строки убрать, живые оставить. Хаб никогда не трогать."""
    hubs = {int(channel_id) for channel_id in hub_ids}
    live = {int(channel_id) for channel_id in existing_channel_ids}
    delete: list[int] = []
    drop: list[int] = []
    keep: list[int] = []
    seen: set[int] = set()
    for room in rooms:
        channel_id = int(room.channel_id)
        if channel_id in hubs or channel_id in seen:
            continue
        seen.add(channel_id)
        if channel_id not in live:
            drop.append(channel_id)
            continue
        if occupied.get(channel_id, 0) > 0:
            keep.append(channel_id)
            continue
        delete.append(channel_id)
    return RoomReconcilePlan(tuple(delete), tuple(drop), tuple(keep))


class NameReject(StrEnum):
    EMPTY = "empty"
    INVITE = "invite"
    AD = "ad"


class ClaimVerdict(StrEnum):
    OK = "ok"
    ALREADY_OWNER = "already_owner"
    OWNER_HERE = "owner_here"
    TOO_SOON = "too_soon"
    HAS_ROOM = "has_room"
    NOT_IN_ROOM = "not_in_room"


class JoinAsk(StrEnum):
    OK = "ok"
    OPEN = "open"
    PENDING = "pending"
    KICKED = "kicked"
    OWNER = "owner"
    INSIDE = "inside"


class LfgAsk(StrEnum):
    OK = "ok"
    IDLE = "idle"
    FULL = "full"
    INSIDE = "inside"
    DEAD = "dead"


class MergeAsk(StrEnum):
    OK = "ok"
    SELF = "self"
    MISSING = "missing"
    PENDING = "pending"


def parse_hub_settings(raw: str) -> HubSettings:
    if not raw or not raw.strip():
        return HubSettings()
    try:
        data = json.loads(raw)
    except ValueError:
        return HubSettings()
    if not isinstance(data, dict):
        return HubSettings()
    prefix = data.get("prefix", "")
    template = data.get("template", NAME_TEMPLATE_DEFAULT)
    preset = data.get("preset", "")
    tid = data.get("template_channel_id")
    extras_raw = data.get("extra_category_ids", ())
    if not isinstance(prefix, str):
        prefix = ""
    if not isinstance(template, str) or not template.strip():
        template = NAME_TEMPLATE_DEFAULT
    numbered = preset == _PRESET_SEQUENTIAL
    if numbered:
        preset = HubPreset.NAME
    elif preset not in {HubPreset.NAME, HubPreset.CLONE}:
        preset = ""
    channel_id = tid if isinstance(tid, int) and tid > 0 else None
    cleaned = " ".join(template.split()) or NAME_TEMPLATE_DEFAULT
    if numbered or "{n}" in cleaned:
        cleaned = strip_seq_placeholder(cleaned)
    extra_ids: list[int] = []
    if isinstance(extras_raw, list):
        for item in extras_raw:
            if isinstance(item, int) and item > 0 and item not in extra_ids:
                extra_ids.append(item)
    return HubSettings(
        prefix=" ".join(prefix.split()),
        template=cleaned,
        preset=preset,
        template_channel_id=channel_id,
        extra_category_ids=tuple(extra_ids),
    )


def dump_hub_settings(settings: HubSettings) -> str:
    data: dict[str, object] = {}
    if settings.prefix:
        data["prefix"] = settings.prefix
    if settings.template and settings.template != NAME_TEMPLATE_DEFAULT:
        data["template"] = settings.template
    if settings.preset:
        data["preset"] = settings.preset
    if settings.template_channel_id:
        data["template_channel_id"] = settings.template_channel_id
    if settings.extra_category_ids:
        data["extra_category_ids"] = list(settings.extra_category_ids)
    return json.dumps(data, ensure_ascii=False) if data else "{}"


def clone_uses_template(*, preset: str, template_channel_id: int | None, hub_id: int) -> bool:
    return preset == HubPreset.CLONE and template_channel_id is not None and template_channel_id != hub_id


def render_room_name(template: str, *, username: str, game: str) -> str:
    filled = template.replace("{username}", username or "комната").replace("{game}", game or username or "комната")
    return room_channel_name(filled)


def prepare_room_name(settings: HubSettings, *, username: str, game: str) -> tuple[str, HubSettings]:
    template = strip_seq_placeholder(settings.template) if "{n}" in settings.template else settings.template
    rendered = render_room_name(template, username=username, game=game)
    checked = check_room_name(rendered, prefix=settings.prefix)
    name = checked.name if checked.ok else room_channel_name(username)
    updated = HubSettings(
        prefix=settings.prefix,
        template=settings.template,
        preset=settings.preset,
        template_channel_id=settings.template_channel_id,
        extra_category_ids=settings.extra_category_ids,
    )
    return name, updated


def inspect_room_name(raw: str) -> NameReject | None:
    text = " ".join(raw.split())
    if not text:
        return NameReject.EMPTY
    lowered = text.lower()
    if _INVITE_RE.search(text) or "discord.gg" in lowered:
        return NameReject.INVITE
    if _URL_RE.search(text) or _AD_RE.search(text):
        return NameReject.AD
    return None


def check_room_name(raw: str, *, prefix: str = "", limit: int = CHANNEL_NAME_LIMIT) -> NameCheck:
    reason = inspect_room_name(raw)
    if reason is not None:
        return NameCheck(False, reason=reason.value)
    text = " ".join(raw.split())
    pref = " ".join(prefix.split())
    if pref and not text.lower().startswith(pref.lower()):
        text = f"{pref} {text}"
    return NameCheck(True, name=room_channel_name(text, limit=limit))


def parse_voice_limit(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return 0
    if not text.isdigit():
        return None
    value = int(text)
    if value > VOICE_LIMIT_MAX:
        return None
    return value


def claim_verdict(
    *,
    actor_id: int,
    owner_id: int,
    owner_in_channel: bool,
    actor_has_room: bool,
    absent_since: datetime | None,
    now: datetime,
    wait: timedelta = CLAIM_AFTER,
) -> ClaimVerdict:
    if actor_id == owner_id:
        return ClaimVerdict.ALREADY_OWNER
    if actor_has_room:
        return ClaimVerdict.HAS_ROOM
    if owner_in_channel:
        return ClaimVerdict.OWNER_HERE
    if absent_since is None or now - absent_since < wait:
        return ClaimVerdict.TOO_SOON
    return ClaimVerdict.OK


def ask_to_join(
    *,
    actor_id: int,
    owner_id: int,
    locked: bool,
    pending: bool,
    kicked: bool,
    inside: bool,
) -> JoinAsk:
    if actor_id == owner_id:
        return JoinAsk.OWNER
    if inside:
        return JoinAsk.INSIDE
    if kicked:
        return JoinAsk.KICKED
    if not locked:
        return JoinAsk.OPEN
    if pending:
        return JoinAsk.PENDING
    return JoinAsk.OK


def kicked_may_enter(*, kicked: bool) -> bool:
    return not kicked


def parse_bitrate(raw: str) -> int | None:
    text = raw.strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value < BITRATE_MIN or value > BITRATE_MAX:
        return None
    return value


def check_room_status(raw: str) -> NameCheck:
    text = " ".join(raw.split())
    if not text:
        return NameCheck(True, name="")
    if len(text) > STATUS_LIMIT:
        return NameCheck(False, reason=NameReject.EMPTY.value)
    reason = inspect_room_name(text)
    if reason is not None and reason is not NameReject.EMPTY:
        return NameCheck(False, reason=reason.value)
    return NameCheck(True, name=text)


def _stamp(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def lfg_is_live(need: int | None, expires_at: datetime | None, *, now: datetime) -> bool:
    if need is None or need < 1 or expires_at is None:
        return False
    return _stamp(now) < _stamp(expires_at)


def parse_lfg_need(raw: str) -> int | None:
    text = raw.strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value < 1 or value > LFG_MAX:
        return None
    return value


def lfg_join(*, live: bool, signed: int, need: int, already: bool) -> LfgAsk:
    if not live:
        return LfgAsk.IDLE
    if already:
        return LfgAsk.INSIDE
    if signed >= need:
        return LfgAsk.FULL
    return LfgAsk.OK


def ask_merge(*, from_id: int, to_id: int, pending_from: int | None) -> MergeAsk:
    if from_id == to_id:
        return MergeAsk.SELF
    if pending_from is not None:
        return MergeAsk.PENDING
    return MergeAsk.OK
