from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import discord
import structlog

from conductor.domain.traffic import MESSAGE_TEMPLATE, Lane, RateProfile, current_lane
from conductor.presentation import copy
from conductor.presentation.layout import COMPONENT_LIMIT, TEXT_LIMIT, collected_text, panel, send_card

log = structlog.get_logger("conductor.log_pump")

ALLOWED_MENTIONS = discord.AllowedMentions.none()
#: Только профиль safe: Discord начинает троттлить канал около 5 постов / 5 с.
MIN_INTERVAL = 0.35
#: Склеивать, если в канале ждёт не меньше стольких карточек.
COALESCE_AT = 3
#: Report a full or broken queue at most this often, so a storm cannot flood the process log.
COMPLAINT_INTERVAL = 60.0


@dataclass(slots=True)
class LogItem:
    channel_id: int
    content: str | None = None
    view: discord.ui.BaseView | None = None
    allowed_mentions: discord.AllowedMentions | None = None
    silent: bool = True


class LogPump:
    """Очередь логов: рабочий на канал, глобальный темп у регулятора.

    Один сломанный элемент не роняет конвейер. Счётчики dropped/failed и жалоба
    не чаще раза в минуту сохраняются.
    """

    def __init__(self, bot: discord.Client, maxsize: int = 2_000) -> None:
        self._bot = bot
        self._queue: asyncio.Queue[LogItem] = asyncio.Queue(maxsize=maxsize)
        self._channels: dict[int, asyncio.Queue[LogItem]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._held: dict[int, LogItem] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self.incidents = 0
        self._last_complaint = 0.0
        self.on_drop: Callable[[], None] | None = None

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._stopping:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="conductor-log-pump")
            self._task.add_done_callback(self._on_worker_done)

    def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
        for task in tuple(self._workers.values()):
            task.cancel()

    def _on_worker_done(self, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        self.incidents += 1
        log.error(
            "log_pump_died",
            incidents=self.incidents,
            error=repr(exc),
            exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
        )
        self.start()

    def _on_channel_done(self, channel_id: int, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        self.incidents += 1
        log.error(
            "log_channel_died",
            channel_id=channel_id,
            incidents=self.incidents,
            error=repr(exc),
            exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
        )
        if channel_id in self._channels and not self._stopping:
            self._ensure_channel(channel_id)

    async def aclose(self, *, drain_timeout: float = 5.0) -> None:
        """Give queued cards a short chance to land, then stop the worker for good."""
        self._stopping = True
        if self._task is None or self._task.done():
            for task in tuple(self._workers.values()):
                task.cancel()
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        self._task.cancel()
        for task in tuple(self._workers.values()):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        for task in tuple(self._workers.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def submit(
        self,
        channel_id: int | None,
        *,
        content: str | None = None,
        view: discord.ui.BaseView | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
        silent: bool = True,
    ) -> None:
        if channel_id is None or (content is None and view is None):
            return
        item = LogItem(
            channel_id=channel_id,
            content=None if view is not None else content,
            view=view,
            allowed_mentions=allowed_mentions,
            silent=silent,
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            callback = self.on_drop
            if callable(callback):
                callback()
            self._complain("log_queue_full", channel_id=channel_id, dropped=self.dropped)

    def _complain(self, event: str, **fields: object) -> None:
        now = time.monotonic()
        if now - self._last_complaint < COMPLAINT_INTERVAL:
            return
        self._last_complaint = now
        log.warning(event, **fields)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            self._channel_queue(item.channel_id).put_nowait(item)
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._channel_queue(nxt.channel_id).put_nowait(nxt)
            for channel_id in tuple(self._channels):
                self._ensure_channel(channel_id)

    def _channel_queue(self, channel_id: int) -> asyncio.Queue[LogItem]:
        queue = self._channels.get(channel_id)
        if queue is None:
            queue = asyncio.Queue()
            self._channels[channel_id] = queue
        return queue

    def _ensure_channel(self, channel_id: int) -> asyncio.Queue[LogItem]:
        queue = self._channels.get(channel_id)
        if queue is None:
            queue = asyncio.Queue()
            self._channels[channel_id] = queue
        task = self._workers.get(channel_id)
        if task is None or task.done():
            worker = asyncio.create_task(self._channel_run(channel_id), name=f"conductor-log-{channel_id}")
            worker.add_done_callback(lambda done, cid=channel_id: self._on_channel_done(cid, done))
            self._workers[channel_id] = worker
        return queue

    async def _channel_run(self, channel_id: int) -> None:
        queue = self._channels[channel_id]
        loop = asyncio.get_running_loop()
        next_send = 0.0
        while True:
            batch = await self._take_batch(channel_id, queue)
            if not batch:
                continue
            try:
                delay = max(next_send - loop.time(), self._channel_wait(channel_id), 0.0)
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._deliver_batch(batch)
                next_send = loop.time() + MIN_INTERVAL if self._safe_profile() else loop.time()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failed += 1
                log.exception("log_item_failed", channel_id=channel_id, failed=self.failed)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _take_batch(self, channel_id: int, queue: asyncio.Queue[LogItem]) -> list[LogItem]:
        held = self._held.pop(channel_id, None)
        first = held if held is not None else await queue.get()
        batch = [first]
        waiting = queue.qsize()
        if waiting + 1 < COALESCE_AT:
            return batch
        while queue.qsize() > 0:
            nxt = queue.get_nowait()
            if _can_fit(batch, nxt):
                batch.append(nxt)
                continue
            self._held[channel_id] = nxt
            break
        return batch

    def _safe_profile(self) -> bool:
        settings = getattr(self._bot, "settings", None)
        profile = getattr(settings, "rate_profile", None)
        return profile is RateProfile.SAFE or str(profile) == RateProfile.SAFE.value

    def _channel_wait(self, channel_id: int) -> float:
        observer = getattr(self._bot, "rate_observer", None)
        state = getattr(observer, "state", None)
        if not callable(state):
            return MIN_INTERVAL if self._safe_profile() else 0.0
        bucket = state(MESSAGE_TEMPLATE, str(channel_id))
        if bucket is None:
            return MIN_INTERVAL if self._safe_profile() else 0.0
        now = time.time()
        reset = max(0.0, float(bucket.reset_after) - (now - float(bucket.at)))
        if int(bucket.remaining) > 0 or reset <= 0:
            return 0.0
        return reset

    async def _deliver_batch(self, items: Sequence[LogItem]) -> None:
        if len(items) == 1:
            await self._deliver(items[0])
            return
        merged = _merge(items)
        await self._deliver(merged)

    async def _deliver(self, item: LogItem) -> None:
        channel = self._bot.get_channel(item.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            self.dropped += 1
            self._complain("log_channel_missing", channel_id=item.channel_id)
            return
        mentions = ALLOWED_MENTIONS if item.allowed_mentions is None else item.allowed_mentions
        try:
            token = current_lane.set(Lane.BACKGROUND)
            try:
                if item.view is not None:
                    await send_card(channel, item.view, mentions=mentions, silent=item.silent)
                else:
                    await channel.send(content=item.content, allowed_mentions=mentions, silent=item.silent)
            finally:
                current_lane.reset(token)
            self.sent += 1
        except discord.Forbidden:
            self.dropped += 1
            self._complain("log_channel_forbidden", channel_id=item.channel_id)
        except discord.HTTPException as exc:
            self.failed += 1
            log.warning("log_send_failed", error=str(exc), status=exc.status, channel_id=item.channel_id)


def _item_text(item: LogItem) -> str:
    if item.view is not None:
        return collected_text(item.view)
    return str(item.content or "")


def _can_fit(batch: Sequence[LogItem], extra: LogItem) -> bool:
    texts = [_item_text(item) for item in batch]
    texts.append(_item_text(extra))
    body = "\n\n".join(chunk for chunk in texts if chunk)
    return len(body) <= TEXT_LIMIT


def _merge(items: Sequence[LogItem]) -> LogItem:
    first = items[0]
    chunks = [_item_text(item) for item in items]
    body = "\n\n".join(chunk for chunk in chunks if chunk)
    if len(body) > TEXT_LIMIT:
        body = body[: TEXT_LIMIT - 1] + "…"
    view = panel(copy.LOG_PACK_TITLE, body, timeout=None)
    if view.total_children_count > COMPONENT_LIMIT:
        return first
    return LogItem(
        channel_id=first.channel_id,
        view=view,
        allowed_mentions=first.allowed_mentions,
        silent=first.silent,
    )
