"""Движение: реестр значков приложения, живой прогресс, отсчёт, каскад.

Бот обязан работать без единого загруженного значка. Реестр — украшение.
Контроллер прогресса пропускает кадры сверх бюджета и не засыпает между ними.
"""

from __future__ import annotations

import asyncio
import struct
import time
import zlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

import discord
import structlog
from discord import ui

from conductor.presentation import copy
from conductor.presentation.icons import ICON_EMOJI
from conductor.presentation.layout import edit_card, edit_response, failure, panel, respond
from conductor.presentation.tokens import Icon, Tone, meter

log = structlog.get_logger("conductor.motion")

MAX_EDITS = 30
MAX_REVEAL_FRAMES = 3
DEFAULT_INTERVAL = 2.0
DEFAULT_SHOW_AFTER = 2.0
_PIXELS = 16
_BAR_NAMES = ("bar_l", "bar_f", "bar_e", "bar_r")

__all__ = [
    "Countdown",
    "DeployReport",
    "EMOJI_PACK_SIZE",
    "EmojiRegistry",
    "InteractionTarget",
    "MessageTarget",
    "Progress",
    "Sendable",
    "StageState",
    "bar",
    "reveal",
]


class StageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAIL = "fail"


class Sendable(Protocol):
    async def edit(self, view: ui.LayoutView) -> None: ...


class InteractionTarget:
    """Правка ответа на слэш: первое видимое сообщение после defer — то же самое."""

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction

    async def edit(self, view: ui.LayoutView) -> None:
        if self.interaction.response.is_done():
            await edit_response(self.interaction, view)
            return
        await respond(self.interaction, view, ephemeral=True)


class MessageTarget:
    """Правка уже отправленного сообщения: чек-лист создания комнаты живёт на нём же."""

    def __init__(self, message: discord.Message) -> None:
        self.message = message

    async def edit(self, view: ui.LayoutView) -> None:
        self.message = await edit_card(self.message, view)


@dataclass(frozen=True, slots=True)
class DeployReport:
    created: int
    skipped: int
    loaded: int
    total: int
    failed: int = 0
    retry_after: float | None = None
    rate_limited: bool = False


@dataclass(frozen=True, slots=True)
class _Spec:
    name: str
    fallback: str
    animated: bool
    png: tuple[int, int, int, int] | None = None
    gif: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
    deployable: bool = False

    def image(self) -> bytes:
        if self.animated:
            frames = self.gif or ((88, 101, 242), (78, 93, 148))
            return _gif_frames(frames)
        return _png(self.png or (88, 101, 242, 255))


def _motion(name: str, fallback: str, *, animated: bool = False, rgb: tuple[int, int, int]) -> _Spec:
    if animated:
        return _Spec(name, fallback, True, gif=(rgb, (78, 93, 148)), deployable=True)
    return _Spec(name, fallback, False, png=(*rgb, 255), deployable=True)


def _art(name: str, fallback: str = "") -> _Spec:
    return _Spec(name, fallback, False, deployable=False)


EMOJI_SPECS: tuple[_Spec, ...] = (
    _motion("load", "⏳", animated=True, rgb=(88, 101, 242)),
    _motion("dots", "…", animated=True, rgb=(153, 170, 181)),
    _motion("tick", Icon.DONE, rgb=(87, 242, 135)),
    _motion("cross", Icon.DENY, rgb=(237, 66, 69)),
    _motion("bar_l", "▰", rgb=(88, 101, 242)),
    _motion("bar_f", "▰", rgb=(88, 101, 242)),
    _motion("bar_e", "▱", rgb=(78, 93, 148)),
    _motion("bar_r", "▱", rgb=(78, 93, 148)),
    _art("ico_panel", Icon.PANEL),
    _art("ico_warn", Icon.WARN),
    _art("ico_lock", Icon.LOCK),
    _art("ico_case", Icon.CASE),
    _art("ico_star", Icon.STAR),
    _art("ico_shift", Icon.SHIFT),
    _art("ico_step", Icon.STEP),
    _art("gem_ruby"),
    _art("gem_emerald"),
    _art("gem_sapphire"),
    _art("gem_amethyst"),
    _art("lad_guest"),
    _art("lad_regular"),
    _art("lad_resident"),
    _art("lad_friend"),
    _art("rnk_watch"),
    _art("rnk_senior"),
    _art("rnk_chief"),
    _art("rnk_special"),
    _art("vc_lock"),
    _art("vc_open"),
    _art("vc_ghost"),
    _art("vc_show"),
)
_SPECS_BY_NAME = {spec.name: spec for spec in EMOJI_SPECS}
EMOJI_PACK_SIZE = len(EMOJI_SPECS)


class EmojiRegistry:
    """Значки приложения плюс текстовый запасной набор."""

    def __init__(self) -> None:
        self._loaded: dict[str, discord.PartialEmoji] = {}

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    @property
    def total_count(self) -> int:
        return len(EMOJI_SPECS)

    @property
    def using_fallback(self) -> bool:
        return self.loaded_count < self.total_count

    def render(self, name: str) -> str:
        found = self._loaded.get(name)
        if found is not None:
            return str(found)
        spec = _SPECS_BY_NAME.get(name)
        return spec.fallback if spec is not None else name

    def resolve(self, mark: str) -> str:
        if not mark:
            return ""
        if mark.startswith("<") and ":" in mark:
            return mark
        return self.render(ICON_EMOJI.get(mark, mark))

    def partial(self, name: str) -> discord.PartialEmoji | None:
        return self._loaded.get(name)

    def bar(self, done: int, total: int, width: int = 10) -> str:
        if any(name not in self._loaded for name in _BAR_NAMES):
            return meter(done, total, width)
        if width < 1:
            width = 1
        if total <= 0:
            filled = 0
            pct = 0
        else:
            clamped = min(max(done, 0), total)
            filled = min(max(round(width * clamped / total), 0), width)
            pct = round(100 * clamped / total)
        parts: list[str] = []
        for index in range(width):
            if index == 0:
                parts.append(self.render("bar_l" if filled else "bar_e"))
            elif index == width - 1:
                parts.append(self.render("bar_r"))
            else:
                parts.append(self.render("bar_f" if index < filled else "bar_e"))
        return f"{''.join(parts)}  {pct}%"

    async def refresh(self, bot: discord.Client) -> None:
        try:
            emojis = await bot.fetch_application_emojis()
        except discord.HTTPException:
            log.exception("motion_emoji_refresh_failed")
            self._loaded.clear()
            return
        by_name = {emoji.name: emoji for emoji in emojis}
        loaded: dict[str, discord.PartialEmoji] = {}
        for spec in EMOJI_SPECS:
            found = by_name.get(spec.name)
            if found is None:
                continue
            loaded[spec.name] = discord.PartialEmoji(
                name=found.name,
                id=found.id,
                animated=found.animated,
            )
        self._loaded = loaded

    async def deploy(self, bot: discord.Client) -> DeployReport:
        try:
            existing = {emoji.name: emoji for emoji in await bot.fetch_application_emojis()}
        except discord.HTTPException:
            log.exception("motion_emoji_fetch_failed")
            return DeployReport(created=0, skipped=0, loaded=self.loaded_count, total=self.total_count, failed=1)
        created = 0
        skipped = 0
        failed = 0
        for spec in EMOJI_SPECS:
            if spec.name in existing:
                skipped += 1
                continue
            if not spec.deployable:
                continue
            try:
                await bot.create_application_emoji(name=spec.name, image=spec.image())
                created += 1
            except discord.RateLimited as exc:
                failed += 1
                await self.refresh(bot)
                return DeployReport(
                    created=created,
                    skipped=skipped,
                    loaded=self.loaded_count,
                    total=self.total_count,
                    failed=failed,
                    retry_after=max(0.0, float(exc.retry_after)),
                    rate_limited=True,
                )
            except discord.HTTPException as exc:
                retry = getattr(exc, "retry_after", None)
                if getattr(exc, "status", None) == 429 and retry is not None:
                    failed += 1
                    await self.refresh(bot)
                    return DeployReport(
                        created=created,
                        skipped=skipped,
                        loaded=self.loaded_count,
                        total=self.total_count,
                        failed=failed,
                        retry_after=max(0.0, float(retry)),
                    )
                log.exception("motion_emoji_create_failed", name=spec.name)
                failed += 1
        await self.refresh(bot)
        return DeployReport(
            created=created,
            skipped=skipped,
            loaded=self.loaded_count,
            total=self.total_count,
            failed=failed,
        )


def bar(done: int, total: int, width: int = 10, *, registry: EmojiRegistry | None = None) -> str:
    return (registry or EmojiRegistry()).bar(done, total, width)


class Countdown:
    """Обратный отсчёт без правок: живёт на <t:…:R>."""

    def render(self, until: datetime) -> str:
        return discord.utils.format_dt(until, style="R")


class Progress:
    """Живая карточка длинной операции. Сама следит за бюджетом правок."""

    def __init__(
        self,
        target: Sendable,
        *,
        title: str,
        total: int | None = None,
        min_interval: float = DEFAULT_INTERVAL,
        show_after: float = DEFAULT_SHOW_AFTER,
        body: str | None = None,
        footnote: str | None = None,
        unit: str | None = None,
        messages_label: str | None = None,
        registry: EmojiRegistry | None = None,
        clock: Callable[[], float] | None = None,
        code: str = "E-0000",
        max_edits: int = MAX_EDITS,
    ) -> None:
        self._target = target
        self._title = title
        self.total = total
        self._min_interval = min_interval
        self._show_after = show_after
        self._body = body
        self._footnote = footnote
        self._unit = unit
        self._messages_label = messages_label or copy.PROGRESS_COUNT
        self._registry = registry or EmojiRegistry()
        self._clock = clock or time.monotonic
        self._code = code
        self._max_edits = max_edits
        self._started_at: float | None = None
        self._last_edit = 0.0
        self._edits = 0
        self._shown = False
        self._finished = False
        self._done = 0
        self.messages: int | None = None
        self._note: str | None = None
        self._stages: list[tuple[str, StageState]] = []
        self._gate = asyncio.Lock()

    async def __aenter__(self) -> Progress:
        if self._started_at is None:
            self._started_at = self._clock()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        if self._finished:
            return
        if exc_type is not None:
            await self.fail(self._code)
            return
        if self._shown:
            await self.fail(self._code)

    async def start(self) -> None:
        async with self._gate:
            if self._started_at is None:
                self._started_at = self._clock()
            await self._paint_running_locked()

    async def step(
        self,
        done: int,
        note: str | None = None,
        *,
        total: int | None = None,
        messages: int | None = None,
    ) -> None:
        async with self._gate:
            self._done = done
            if note is not None:
                self._note = note
            if total is not None:
                self.total = total
            if messages is not None:
                self.messages = messages
            await self._paint_running_locked()

    async def stage(self, name: str, state: StageState) -> None:
        async with self._gate:
            for index, (current, _) in enumerate(self._stages):
                if current == name:
                    self._stages[index] = (name, state)
                    break
            else:
                self._stages.append((name, state))
            await self._paint_running_locked()

    async def finish(self, view: ui.LayoutView) -> None:
        async with self._gate:
            if self._finished:
                return
            self._finished = True
            await self._send(view, swallow=False)

    async def fail(self, code: str) -> None:
        async with self._gate:
            if self._finished:
                return
            self._finished = True
            await self._send(failure(code), swallow=True)

    async def _paint_running_locked(self) -> None:
        if self._finished:
            return
        now = self._clock()
        if self._started_at is None:
            self._started_at = now
        if now - self._started_at < self._show_after:
            return
        if self._shown:
            if self._edits >= self._max_edits:
                return
            if now - self._last_edit < self._min_interval:
                return
        await self._send(self._running_view(), swallow=True)
        self._shown = True
        self._edits += 1
        self._last_edit = now

    async def _send(self, view: ui.LayoutView, *, swallow: bool) -> None:
        try:
            await self._target.edit(view)
        except discord.HTTPException:
            if swallow:
                return
            raise

    def _running_view(self) -> ui.LayoutView:
        icon = self._registry.render("load")
        body: list[str] = []
        fields: dict[str, object] | None = None
        if self._stages:
            body.extend(self._stage_lines())
        elif self.total:
            body.append(self._registry.bar(self._done, self.total))
            counts = f"{self._done} из {self.total}"
            if self._unit:
                counts = f"{counts} {self._unit}"
            body.append(counts)
            if self.messages is not None:
                fields = {self._messages_label: copy.grouped(self.messages)}
        elif self._body:
            body.append(self._body)
        footnote = self._note if self._note is not None else self._footnote
        return panel(
            self._title,
            body,
            tone=Tone.RUNNING,
            fields=fields,
            footnote=footnote,
            icon=icon,
            timeout=None,
        )

    def _stage_lines(self) -> list[str]:
        marks = {
            StageState.PENDING: self._registry.resolve(Icon.STEP),
            StageState.RUNNING: self._registry.render("dots"),
            StageState.DONE: self._registry.render("tick"),
            StageState.FAIL: self._registry.render("cross"),
        }
        return [f"{marks[state]} {name}" for name, state in self._stages]


async def reveal(
    target: Sendable,
    frames: Sequence[ui.LayoutView],
    *,
    interval: float = DEFAULT_INTERVAL,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Постановочный каскад. Максимум 3 кадра. Только для объявления победителя."""
    pause = sleep or asyncio.sleep
    shown = list(frames)[:MAX_REVEAL_FRAMES]
    for index, frame in enumerate(shown):
        if index:
            await pause(interval)
        await target.edit(frame)


def _png(rgba: tuple[int, int, int, int], size: int = _PIXELS) -> bytes:
    red, green, blue, alpha = rgba
    raw = b"".join(b"\x00" + bytes((red, green, blue, alpha)) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _gif_frames(colors: tuple[tuple[int, int, int], tuple[int, int, int]], size: int = _PIXELS) -> bytes:
    palette = [colors[0], colors[1], (0, 0, 0), (255, 255, 255)]
    packed_gct = 0x80 | 0x01
    out = bytearray(b"GIF89a")
    out += struct.pack("<HH", size, size)
    out += bytes((packed_gct, 0, 0))
    for red, green, blue in palette:
        out += bytes((red, green, blue))
    out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    delay = 12
    for index, _color in enumerate(colors):
        out += b"\x21\xf9\x04\x08"
        out += struct.pack("<H", delay)
        out += b"\x00\x00\x2c"
        out += struct.pack("<HHHH", 0, 0, size, size)
        out += b"\x00\x02"
        raster = [index] * (size * size)
        out += _subblocks(_lzw(raster, 2))
    out += b"\x3b"
    return bytes(out)


def _lzw(indexes: Sequence[int], min_code_size: int) -> bytes:
    clear = 1 << min_code_size
    end = clear + 1
    next_code = end + 1
    code_size = min_code_size + 1
    table: dict[tuple[int, ...], int] = {(value,): value for value in range(clear)}
    codes: list[tuple[int, int]] = [(clear, code_size)]
    prefix: tuple[int, ...] | None = None
    for value in indexes:
        if prefix is None:
            prefix = (value,)
            continue
        candidate = (*prefix, value)
        if candidate in table:
            prefix = candidate
            continue
        codes.append((table[prefix], code_size))
        if next_code < 4096:
            table[candidate] = next_code
            next_code += 1
            if next_code >= (1 << code_size) and code_size < 12:
                code_size += 1
        else:
            codes.append((clear, code_size))
            table = {(item,): item for item in range(clear)}
            next_code = end + 1
            code_size = min_code_size + 1
        prefix = (value,)
    if prefix is not None:
        codes.append((table[prefix], code_size))
    codes.append((end, code_size))
    acc = 0
    bits = 0
    packed = bytearray()
    for code, size in codes:
        acc |= code << bits
        bits += size
        while bits >= 8:
            packed.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        packed.append(acc & 0xFF)
    return bytes(packed)


def _subblocks(data: bytes) -> bytes:
    out = bytearray()
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + 255]
        out.append(len(chunk))
        out += chunk
        offset += 255
    out.append(0)
    return bytes(out)
