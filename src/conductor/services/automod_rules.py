"""Загрузка data/automod.yml и окна счёта в памяти."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import yaml

from conductor.db.models import AutomodHit
from conductor.db.session import session_scope
from conductor.domain.automod import (
    AutomodConfig,
    ConfigError,
    MessageFacts,
    RollingWindow,
    Trigger,
    after_trigger,
    default_config,
    evaluate,
    fill_missing_rules,
    parse_config,
)
from conductor.presentation import copy
from conductor.presentation.layout import event
from conductor.presentation.text import FOOTER_ADMIN
from conductor.presentation.tokens import Tone

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.automod")

FILE_NAME = "automod.yml"
EXAMPLE_NAME = "automod.yml.example"

__all__ = [
    "AutomodRules",
    "LoadResult",
    "automod_path",
    "example_automod_path",
    "load",
]


@dataclass(frozen=True, slots=True)
class LoadResult:
    config: AutomodConfig | None
    errors: tuple[ConfigError, ...]
    mtime: float | None
    missing: bool = False


def example_automod_path(settings: object | None = None) -> Path:
    """Эталон правил. В образе «каталог проекта» — это site-packages, поэтому ищем шире."""
    from conductor.config import PROJECT_ROOT

    candidates = (
        PROJECT_ROOT / "data" / EXAMPLE_NAME,
        automod_path(settings).with_name(EXAMPLE_NAME),
        Path.cwd() / "data" / EXAMPLE_NAME,
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def automod_path(settings: object | None = None) -> Path:
    from conductor.config import DEFAULT_DATA_DIR

    folder = getattr(settings, "data_dir", None)
    if folder is None:
        folder = DEFAULT_DATA_DIR
    return Path(folder) / FILE_NAME


def load(path: Path) -> LoadResult:
    target = Path(path)
    if not target.is_file():
        return LoadResult(config=None, errors=(), mtime=None, missing=True)
    mtime = target.stat().st_mtime
    try:
        text = target.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return LoadResult(None, (ConfigError("", copy.automod_yaml_error(exc)),), mtime)
    except OSError as exc:
        return LoadResult(None, (ConfigError("", copy.automod_file_error(exc)),), mtime)
    parsed = parse_config(raw)
    if isinstance(parsed, list):
        return LoadResult(None, tuple(parsed), mtime)
    return LoadResult(parsed, (), mtime)


class AutomodRules:
    """Текущие правила, окна TTLCache и запись срабатываний."""

    def __init__(self, bot: Conductor, path: Path | None = None) -> None:
        self._bot = bot
        self.path = Path(path) if path is not None else automod_path(getattr(bot, "settings", None))
        self.config = default_config()
        self.file_missing = True
        self.errors: tuple[ConfigError, ...] = ()
        self.mtime: float | None = None
        self.loaded_at: datetime = datetime.now(UTC)
        self._pending: LoadResult | None = None
        self._sync_window_ttl()

    def load(self, path: Path | None = None) -> LoadResult:
        if path is not None:
            self.path = Path(path)
        result = load(self.path)
        if result.missing:
            self.config = default_config()
            self.file_missing = True
            self.errors = ()
            self.mtime = None
            self._pending = None
            self.loaded_at = datetime.now(UTC)
            self._sync_window_ttl()
            log.warning("automod_file_missing", path=str(self.path))
            return result
        if result.errors:
            self.errors = result.errors
            self.mtime = result.mtime
            self.file_missing = False
            self._pending = result
            self.notify_rejected(result)
            log.warning(
                "automod_file_rejected",
                path=str(self.path),
                errors=[str(item) for item in result.errors],
            )
            return result
        assert result.config is not None
        self.config = fill_missing_rules(result.config)
        self.file_missing = False
        self.errors = ()
        self.mtime = result.mtime
        self._pending = None
        self.loaded_at = datetime.now(UTC)
        self._sync_window_ttl()
        log.info("automod_file_loaded", path=str(self.path), rules=len(self.config.rules))
        return result

    def reload(self) -> LoadResult:
        return self.load()

    def state_for(self, guild_id: int, user_id: int) -> RollingWindow:
        cache = self._bot.caches.automod_windows
        key = (int(guild_id), int(user_id))
        found = cache.get(key)
        if found is None:
            found = RollingWindow()
        cache.set(key, found)
        return found

    def evaluate(
        self,
        facts: MessageFacts,
        *,
        now: datetime | None = None,
        only: tuple[str, ...] | None = None,
    ) -> list[Trigger]:
        moment = now if now is not None else datetime.now(UTC)
        return evaluate(
            facts,
            self.state_for(facts.guild_id, facts.user_id),
            self.config,
            moment,
            only=only,
        )

    def after_trigger(self, facts: MessageFacts, rule: str, *, text: str = "") -> None:
        after_trigger(self.state_for(facts.guild_id, facts.user_id), rule, text=text)

    async def record_hit(
        self,
        *,
        guild_id: int,
        user_id: int,
        rule: str,
        channel_id: int,
        message_id: int,
        case_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        moment = now if now is not None else datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            session.add(
                AutomodHit(
                    guild_id=int(guild_id),
                    user_id=int(user_id),
                    rule=str(rule),
                    channel_id=int(channel_id),
                    message_id=int(message_id),
                    case_id=case_id,
                    created_at=moment,
                )
            )

    def notify_rejected(self, result: LoadResult) -> None:
        first = result.errors[0] if result.errors else ConfigError("", copy.AUTOMOD_ERROR)
        view = event(
            copy.AUTOMOD_FILE_REJECTED_TITLE,
            copy.automod_file_rejected(str(self.path), copy.automod_cfg_error(first)),
            tone=Tone.DANGER,
            source=FOOTER_ADMIN,
        )
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return
        posted = False
        for guild in getattr(self._bot, "guilds", ()) or ():
            guild_id = int(getattr(guild, "id", 0) or 0)
            if not guild_id or not getattr(self._bot, "serves", lambda _gid: True)(guild_id):
                continue
            config = getattr(self._bot, "config_by_guild", {}).get(guild_id)
            channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
            if channel_id is None:
                continue
            submit(int(channel_id), view=view)
            posted = True
        if posted:
            self._pending = None

    def flush_notices(self) -> None:
        if self._pending is None:
            return
        self.notify_rejected(self._pending)

    def _sync_window_ttl(self) -> None:
        caches = getattr(self._bot, "caches", None)
        setter = getattr(caches, "set_automod_window_ttl", None)
        if callable(setter):
            setter(float(self.config.window_minutes) * 60.0)
