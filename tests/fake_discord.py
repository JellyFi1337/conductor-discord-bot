"""Поддельный Discord HTTP для тестов. Внутренности discord.py допустимы только здесь."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from typing import Any

import aiohttp
from aiohttp import web
from discord.http import HTTPClient, Route

from conductor.discord_compat import install_ratelimit_compat
from conductor.domain.traffic import is_interaction_route, route_key

_JSON = "application/json"
_VIA = "1.1 discord"


class FakeDiscord:
    """Сервер с лимитами маршрутов, глобальным потолком и скрытым 429."""

    def __init__(
        self,
        *,
        global_limit: int = 50,
        global_window: float = 1.0,
        route_limit: int = 10,
        route_window: float = 10.0,
        hidden_after: int | None = None,
        hidden_retry_after: float = 45.0,
        scope: str = "user",
        force_status: int | None = None,
        force_global: bool = False,
    ) -> None:
        self.global_limit = int(global_limit)
        self.global_window = float(global_window)
        self.route_limit = int(route_limit)
        self.route_window = float(route_window)
        self.hidden_after = hidden_after
        self.hidden_retry_after = float(hidden_retry_after)
        self.scope = str(scope)
        self.force_status = force_status
        self.force_global = bool(force_global)
        self.global_retry_after = 45.0
        self.requests: list[tuple[str, str]] = []
        self.statuses: list[int] = []
        self._global: deque[float] = deque()
        self._routes: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._creates: dict[str, deque[float]] = defaultdict(deque)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._base = ""
        self._old_base = Route.BASE
        self._http: HTTPClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def base(self) -> str:
        return self._base

    async def __aenter__(self) -> FakeDiscord:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        self._loop = asyncio.get_running_loop()
        first = self._runner.addresses[0]
        if isinstance(first, tuple):
            port = int(first[1])
        elif hasattr(first, "port"):
            port = int(first.port or 0)
        else:
            port = int(str(first).rsplit(":", 1)[-1])
        self._base = f"http://127.0.0.1:{port}/api/v10"
        self._old_base = Route.BASE
        Route.BASE = self._base
        return self

    async def __aexit__(self, *_exc: object) -> None:
        Route.BASE = self._old_base
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def http(
        self,
        *,
        trace: aiohttp.TraceConfig | None = None,
        max_ratelimit_timeout: float = 30.0,
    ) -> HTTPClient:
        install_ratelimit_compat()
        loop = self._loop or asyncio.get_running_loop()
        client = HTTPClient(
            loop,
            http_trace=trace,
            max_ratelimit_timeout=max_ratelimit_timeout,
        )
        client._global_over = asyncio.Event()
        client._global_over.set()
        traces = [] if trace is None else [trace]
        session = aiohttp.ClientSession(trace_configs=traces)
        client._HTTPClient__session = session
        self._http = client
        return client

    async def _handle(self, request: web.Request) -> web.Response:
        now = (self._loop or asyncio.get_running_loop()).time()
        method = request.method
        path = request.path
        self.requests.append((method, path))
        template, major = route_key(method, path)
        if self.force_status == 403:
            self.statuses.append(403)
            return self._json(403, {"message": "Missing Permissions", "code": 50013})
        if self.force_status == 401:
            self.statuses.append(401)
            return self._json(401, {"message": "401: Unauthorized", "code": 0})
        interaction = is_interaction_route(template)
        if not interaction:
            self._trim(self._global, now, self.global_window)
            if len(self._global) >= self.global_limit or self.force_global:
                window = max(self.global_window, self.global_retry_after)
                delay = self.global_retry_after if self.force_global else window
                return self._rate_limited(now, retry_after=delay, remaining=0, global_hit=True)
            self._global.append(now)
        key = (template, major)
        stamps = self._routes[key]
        self._trim(stamps, now, self.route_window)
        remaining = max(0, self.route_limit - len(stamps) - 1)
        reset_after = self.route_window if not stamps else max(0.0, stamps[0] + self.route_window - now)
        if self.hidden_after is not None and template.startswith("POST /guilds/{id}/channels"):
            created = self._creates[major]
            self._trim(created, now, self.route_window)
            if len(created) >= self.hidden_after:
                return self._rate_limited(
                    now,
                    retry_after=self.hidden_retry_after,
                    remaining=max(remaining, 1),
                    limit=self.route_limit,
                    reset_after=self.route_window,
                )
            created.append(now)
        if len(stamps) >= self.route_limit:
            return self._rate_limited(
                now,
                retry_after=max(reset_after, 0.01),
                remaining=0,
                limit=self.route_limit,
                reset_after=max(reset_after, 0.01),
            )
        stamps.append(now)
        remaining = max(0, self.route_limit - len(stamps))
        payload: dict[str, Any] = {"id": "1", "type": 0, "name": "channel"}
        if path.rstrip("/").endswith("@me"):
            payload = {"id": "1", "username": "bot", "bot": True}
        code = 200 if method == "GET" else 201
        self.statuses.append(code)
        return self._json(
            code,
            payload,
            extra={
                "X-RateLimit-Limit": str(self.route_limit),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset-After": f"{max(reset_after, 0.0):.3f}",
                "X-RateLimit-Bucket": "fake",
                "X-RateLimit-Scope": self.scope,
            },
        )

    def _rate_limited(
        self,
        now: float,
        *,
        retry_after: float,
        remaining: int,
        global_hit: bool = False,
        limit: int | None = None,
        reset_after: float | None = None,
    ) -> web.Response:
        del now
        cap = self.route_limit if limit is None else limit
        reset = retry_after if reset_after is None else reset_after
        body = {
            "message": "You are being rate limited.",
            "retry_after": float(retry_after),
            "global": bool(global_hit),
        }
        extra = {
            "X-RateLimit-Limit": str(cap),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset-After": f"{float(reset):.3f}",
            "X-RateLimit-Bucket": "fake",
            "X-RateLimit-Scope": "global" if global_hit else self.scope,
            "X-RateLimit-Global": "true" if global_hit else "false",
            "Retry-After": f"{float(retry_after):.3f}",
        }
        self.statuses.append(429)
        return self._json(429, body, extra=extra)

    def _json(self, status: int, payload: dict[str, Any], *, extra: dict[str, str] | None = None) -> web.Response:
        response = web.Response(status=status, body=json.dumps(payload).encode("utf-8"))
        response.headers["Content-Type"] = _JSON
        response.headers["Via"] = _VIA
        if extra:
            for key, value in extra.items():
                response.headers[key] = value
        return response

    def _trim(self, stamps: deque[float], now: float, window: float) -> None:
        cutoff = now - window
        while stamps and stamps[0] < cutoff:
            stamps.popleft()


__all__ = ["FakeDiscord"]
