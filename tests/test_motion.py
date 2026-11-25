from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from conductor.presentation.copy import FAILURE_TITLE, INGEST_TITLE
from conductor.presentation.icons import bind, marked, render_icon
from conductor.presentation.layout import collected_text, panel
from conductor.presentation.motion import (
    EMOJI_PACK_SIZE,
    MAX_EDITS,
    Countdown,
    EmojiRegistry,
    Progress,
    bar,
    reveal,
)
from conductor.presentation.tokens import Icon, Tone, meter


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class MemoryTarget:
    def __init__(self) -> None:
        self.views: list[discord.ui.LayoutView] = []

    async def edit(self, view: discord.ui.LayoutView) -> None:
        self.views.append(view)


def _text(view: discord.ui.LayoutView) -> str:
    return collected_text(view)


@pytest.mark.asyncio
async def test_budget_skips_extra_frames_without_stalling_work() -> None:
    clock = FakeClock()
    target = MemoryTarget()
    progress = Progress(target, title=INGEST_TITLE, show_after=0, min_interval=2.0, clock=clock)
    started = time.perf_counter()
    await progress.start()
    for done in range(20):
        clock.advance(0.05)
        await progress.step(done)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert len(target.views) == 1
    clock.advance(2.0)
    await progress.step(40)
    assert len(target.views) == 2
    done = panel("История собрана", tone=Tone.OK)
    await progress.finish(done)
    assert target.views[-1] is done


@pytest.mark.asyncio
async def test_first_and_last_frames_are_always_delivered() -> None:
    clock = FakeClock()
    target = MemoryTarget()
    progress = Progress(
        target,
        title=INGEST_TITLE,
        show_after=0,
        min_interval=0,
        clock=clock,
        max_edits=MAX_EDITS,
    )
    for done in range(40):
        await progress.step(done)
        clock.advance(1)
    assert len(target.views) == MAX_EDITS
    final = panel("История собрана", tone=Tone.OK)
    await progress.finish(final)
    assert len(target.views) == MAX_EDITS + 1
    assert target.views[0] is not final
    assert target.views[-1] is final
    assert "История собрана" in _text(final)


@pytest.mark.asyncio
async def test_context_manager_exception_calls_fail() -> None:
    clock = FakeClock()
    target = MemoryTarget()
    with pytest.raises(RuntimeError, match="boom"):
        async with Progress(target, title=INGEST_TITLE, show_after=0, clock=clock, code="E-ABCD") as progress:
            await progress.start()
            raise RuntimeError("boom")
    assert target.views
    text = _text(target.views[-1])
    assert FAILURE_TITLE in text
    assert "E-ABCD" in text
    assert "⏳" not in text.splitlines()[-1]


@pytest.mark.asyncio
async def test_short_operation_does_not_draw_progress() -> None:
    clock = FakeClock()
    target = MemoryTarget()
    async with Progress(target, title=INGEST_TITLE, show_after=2.0, clock=clock) as progress:
        await progress.start()
        clock.advance(1.0)
        await progress.step(3, note="сейчас: #общий")
        final = panel("История собрана", tone=Tone.OK)
        await progress.finish(final)
    assert len(target.views) == 1
    text = _text(target.views[0])
    assert INGEST_TITLE not in text
    assert "История собрана" in text
    assert "⏳" not in text


def test_fallback_renders_without_application_emojis() -> None:
    registry = EmojiRegistry()
    assert registry.render("load") == "⏳"
    assert registry.render("dots") == "…"
    assert registry.render("tick") == "✓"
    assert registry.render("cross") == "✕"
    assert registry.render("gem_ruby") == ""
    assert registry.resolve(Icon.PANEL) == Icon.PANEL
    assert registry.bar(7, 10) == meter(7, 10)
    assert bar(7, 10) == meter(7, 10)
    assert registry.using_fallback is True
    assert registry.total_count == EMOJI_PACK_SIZE


def test_resolve_uses_uploaded_application_emojis() -> None:
    registry = EmojiRegistry()
    registry._loaded = {  # noqa: SLF001
        "ico_panel": discord.PartialEmoji(name="ico_panel", id=11, animated=False),
        "gem_ruby": discord.PartialEmoji(name="gem_ruby", id=22, animated=False),
        "load": discord.PartialEmoji(name="load", id=33, animated=True),
    }
    bind(resolve=registry.resolve, button=registry.partial)
    assert registry.resolve(Icon.PANEL) == "<:ico_panel:11>"
    assert render_icon(Icon.PANEL) == "<:ico_panel:11>"
    assert render_icon(Icon.WAIT) == "<a:load:33>"
    assert marked("gem_ruby", "Ruby Old") == "<:gem_ruby:22> Ruby Old"
    assert marked("gem_emerald", "Emerald Old") == "Emerald Old"
    heading = collected_text(panel("Режим бота"))
    assert heading.splitlines()[0] == "## <:ico_panel:11> Режим бота"


def test_bar_uses_segment_emojis_when_loaded() -> None:
    registry = EmojiRegistry()
    registry._loaded = {  # noqa: SLF001
        name: discord.PartialEmoji(name=name, id=index + 1, animated=False)
        for index, name in enumerate(("bar_l", "bar_f", "bar_e", "bar_r"))
    }
    drawn = registry.bar(7, 10)
    assert "70%" in drawn
    assert "<:bar_l:1>" in drawn
    assert "<:bar_f:2>" in drawn
    assert "<:bar_e:3>" in drawn
    assert "<:bar_r:4>" in drawn


def test_countdown_is_relative_timestamp_without_requests() -> None:
    instant = datetime(2024, 4, 5, 12, 0, tzinfo=UTC)
    rendered = Countdown().render(instant)
    assert rendered == discord.utils.format_dt(instant, style="R")
    assert rendered.startswith("<t:") and rendered.endswith(":R>")


@pytest.mark.asyncio
async def test_reveal_caps_at_three_frames() -> None:
    target = MemoryTarget()
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    frames = [panel(f"кадр {index}") for index in range(5)]
    await reveal(target, frames, interval=2.0, sleep=fake_sleep)
    assert len(target.views) == 3
    assert slept == [2.0, 2.0]
    assert "кадр 0" in _text(target.views[0])
    assert "кадр 2" in _text(target.views[-1])
    assert "кадр 3" not in _text(target.views[-1])


@pytest.mark.asyncio
async def test_deploy_is_idempotent() -> None:
    created: list[str] = []
    existing: list[SimpleNamespace] = []

    class FakeBot:
        async def fetch_application_emojis(self) -> list[SimpleNamespace]:
            return list(existing)

        async def create_application_emoji(self, *, name: str, image: bytes) -> SimpleNamespace:
            assert image[:4] in {b"\x89PNG", b"GIF8"}
            emoji = SimpleNamespace(name=name, id=len(existing) + 1, animated=name in {"load", "dots"})
            existing.append(emoji)
            created.append(name)
            return emoji

    registry = EmojiRegistry()
    first = await registry.deploy(FakeBot())  # type: ignore[arg-type]
    assert first.created == 8
    assert first.skipped == 0
    assert first.loaded == 8
    assert first.total == EMOJI_PACK_SIZE
    second = await registry.deploy(FakeBot())  # type: ignore[arg-type]
    assert second.created == 0
    assert second.skipped == 8
    assert created == ["load", "dots", "tick", "cross", "bar_l", "bar_f", "bar_e", "bar_r"]
