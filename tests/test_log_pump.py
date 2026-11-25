import asyncio
from unittest.mock import MagicMock

import discord

from conductor.presentation.layout import collected_text
from conductor.services.log_pump import LogPump


class _Channel(discord.abc.Messageable):
    def __init__(self, *, explode: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self._explode = explode

    async def _get_channel(self):  # pragma: no cover - satisfies the ABC
        return self

    async def send(self, **kwargs):
        if self._explode is not None:
            raise self._explode
        self.sent.append(kwargs)


def _pump(channel) -> LogPump:
    bot = MagicMock()
    bot.get_channel.return_value = channel
    pump = LogPump(bot)
    return pump


async def _drain(pump: LogPump) -> None:
    pump.start()
    await asyncio.wait_for(pump._queue.join(), timeout=2)
    await pump.aclose(drain_timeout=0.1)


async def test_pump_sends_queued_cards() -> None:
    channel = _Channel()
    pump = _pump(channel)
    pump.submit(1, content="hello")
    await _drain(pump)
    assert len(channel.sent) == 1
    assert channel.sent[0]["silent"] is True
    assert channel.sent[0]["allowed_mentions"].everyone is False


async def test_one_broken_item_does_not_kill_the_pump() -> None:
    """A malformed payload used to raise out of the worker and silence every later log."""
    channel = _Channel(explode=TypeError("bad embed"))
    pump = _pump(channel)
    pump.submit(1, content="boom")
    pump.submit(1, content="still here")
    await _drain(pump)
    assert pump.failed == 2
    assert pump._task is not None


async def test_missing_channel_is_dropped_not_raised() -> None:
    pump = _pump(None)
    pump.submit(1, content="nowhere")
    await _drain(pump)
    assert pump.dropped == 1


async def test_full_queue_counts_drops_instead_of_blocking() -> None:
    bot = MagicMock()
    bot.get_channel.return_value = _Channel()
    pump = LogPump(bot, maxsize=2)
    for _ in range(5):
        pump.submit(1, content="x")
    assert pump.backlog == 2
    assert pump.dropped == 3


def test_full_queue_notifies_on_drop() -> None:
    hits: list[int] = []
    pump = LogPump(MagicMock(), maxsize=1)
    pump.on_drop = lambda: hits.append(1)
    pump.submit(1, content="kept")
    pump.submit(1, content="lost")
    assert pump.dropped == 1
    assert hits == [1]


async def test_three_waiting_cards_are_sent_as_one() -> None:
    channel = _Channel()
    pump = _pump(channel)
    pump.submit(1, content="alpha")
    pump.submit(1, content="beta")
    pump.submit(1, content="gamma")
    await _drain(pump)
    assert len(channel.sent) == 1
    view = channel.sent[0]["view"]
    text = collected_text(view)
    assert "alpha" in text
    assert "beta" in text
    assert "gamma" in text


async def test_submit_forwards_allowed_mentions_and_silent() -> None:
    channel = _Channel()
    pump = _pump(channel)
    mentions = discord.AllowedMentions(roles=[discord.Object(id=33)])
    pump.submit(1, content="ping", allowed_mentions=mentions, silent=False)
    await _drain(pump)
    assert channel.sent[0]["silent"] is False
    assert channel.sent[0]["allowed_mentions"] is mentions


async def test_layout_view_is_sent_without_content_or_embed() -> None:
    channel = _Channel()
    pump = _pump(channel)
    view = discord.ui.LayoutView(timeout=None)
    pump.submit(1, view=view)
    await _drain(pump)
    assert "content" not in channel.sent[0]
    assert "embed" not in channel.sent[0]
    assert channel.sent[0]["view"] is view


async def test_dead_worker_is_restarted_and_counted() -> None:
    channel = _Channel()
    pump = _pump(channel)
    real_run = pump._run
    calls = {"n": 0}

    async def gated() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pump-dead")
        await real_run()

    pump._run = gated  # type: ignore[method-assign]
    pump.start()
    for _ in range(50):
        if pump.incidents == 1 and pump.alive:
            break
        await asyncio.sleep(0.02)
    assert pump.incidents == 1
    assert pump.alive
    await pump.aclose(drain_timeout=0.1)
    assert not pump.alive
