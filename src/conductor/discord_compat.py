"""Локальная совместимость с limiter discord.py 2.7.x.

Upstream: https://github.com/Rapptz/discord.py/blob/v2.7.1/discord/http.py
Ratelimit.acquire и __aexit__ применяют max_ratelimit_timeout даже при
положительном остатке. Длинное окно не означает, что надо ждать его сброса.
Порог ожидания должен действовать только когда свободных жетонов нет.

Изолировано здесь: сервисы не меняют HTTPClient и его внутренние счётчики.
При обновлении discord.py этот адаптер требует проверки и удаления после
исправления upstream.

Portions adapted from discord.py, Copyright (c) 2015-present Rapptz.
The MIT License (MIT)

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
from types import TracebackType

import aiohttp
import discord
import discord.http
import structlog

log = structlog.get_logger("conductor.discord_compat")

_UpstreamRatelimit = discord.http.Ratelimit


class AvailableTokenRatelimit(_UpstreamRatelimit):
    """Сохраняет ограничения Discord и бюджет ожидания исчерпанного ведра."""

    __slots__ = ()

    def update(self, response: aiohttp.ClientResponse, *, use_clock: bool = False) -> None:
        remaining, dirty = self.remaining, self.dirty
        super().update(response, use_clock=use_clock)
        # Ответы могут приходить в другом порядке. Заголовок одного запроса
        # не должен возвращать жетоны, уже зарезервированные соседними.
        if dirty:
            self.remaining = min(remaining, self.remaining)
        else:
            self.remaining = max(0, self.remaining - max(0, self.outgoing - 1))

    async def acquire(self) -> None:
        self._last_request = self._loop.time()
        if self.is_expired():
            self.reset()
        while self.remaining <= 0:
            wait = self.expires - self._loop.time() if self.expires is not None else 30.0
            if self._max_ratelimit_timeout is not None and wait > self._max_ratelimit_timeout:
                raise discord.RateLimited(wait)
            future = self._loop.create_future()
            self._pending_requests.append(future)
            try:
                # await future также передаёт RateLimited от исчерпанного ведра.
                # asyncio.wait в 2.7.x оставляет исключение future непрочитанным.
                async with asyncio.timeout(max(0.0, wait)):
                    await future
            except TimeoutError:
                await self._refresh()
            except BaseException:
                future.cancel()
                if self.remaining > 0:
                    self._wake_next()
                raise
        # Без await: проверка и резервирование слота атомарны в event loop.
        self.remaining -= 1
        self.outgoing += 1

    async def _refresh(self) -> None:
        async with self._sleeping:
            if self.expires is None and self.outgoing:
                return  # Первый ответ ещё не сообщил параметры окна.
            while self.expires is not None:
                wait = self.expires - self._loop.time()
                if wait <= 0:
                    break
                if self._max_ratelimit_timeout is not None and wait > self._max_ratelimit_timeout:
                    # Не восстанавливаем жетоны до настоящего сброса окна.
                    self._wake(len(self._pending_requests), exception=discord.RateLimited(wait))
                    return
                await asyncio.sleep(wait)
            # Другая корутина могла уже сбросить окно, пока мы ждали lock.
            if self.expires is not None or self.remaining <= 0 and not self.outgoing:
                self.reset()
            self._wake(self.remaining)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.outgoing -= 1
        if self.expires is None and not self.outgoing and self.remaining <= 0:
            self.reset()  # Запрос завершился без заголовков лимита.
        # Ожидание выполняется перед следующим запросом. Успешный ответ надо
        # сразу вернуть сервису, чтобы созданный объект получил привязку в БД.
        # Очередь перечитает остаток и срок окна внутри acquire без await между
        # проверкой доступности и резервированием жетона.
        self._wake(len(self._pending_requests))


def install_ratelimit_compat() -> None:
    """Устанавливается до создания HTTPClient; не меняет живые buckets."""
    if discord.http.Ratelimit is AvailableTokenRatelimit:
        return
    if discord.version_info[:2] != (2, 7):
        raise RuntimeError("Проверьте адаптер лимитов Conductor для новой версии discord.py")
    if discord.http.Ratelimit is not _UpstreamRatelimit:
        raise RuntimeError("Обнаружен другой адаптер лимитов discord.py")
    discord.http.Ratelimit = AvailableTokenRatelimit
    log.info("discord_ratelimit_compat_enabled", discord_version=discord.__version__)
