from __future__ import annotations

from types import SimpleNamespace

import discord
from discord import ui

from conductor.presentation.layout import Card, collected_text, panel
from conductor.services.broadcast import BroadcastReport, ping_many


class _Channel:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.payloads: list[dict[str, object]] = []
        self.fail_on = fail_on
        self.n = 0
        self.guild = SimpleNamespace(chunked=False, get_member=lambda uid: SimpleNamespace(id=uid))

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.n += 1
        if self.fail_on == self.n:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="fail"), "fail")
        self.payloads.append(kwargs)
        return SimpleNamespace(id=self.n)


def _header() -> ui.LayoutView:
    return panel("Пинг", "организатор зовёт")


async def test_ping_many_sends_header_and_three_batches() -> None:
    channel = _Channel()
    ids = list(range(1, 251))
    progress: list[tuple[int, int]] = []

    async def mark(index: int, total: int) -> None:
        progress.append((index, total))

    report = await ping_many(SimpleNamespace(), channel, ids, header=_header(), progress=mark)
    assert report == BroadcastReport(batches=3, sent=3, failed=0, users=250)
    assert len(channel.payloads) == 4
    assert "организатор зовёт" in collected_text(channel.payloads[0]["view"])  # type: ignore[arg-type]
    sizes: list[int] = []
    for payload in channel.payloads[1:]:
        mentions = payload["allowed_mentions"]
        users = list(mentions.users)
        assert mentions.everyone is False
        assert len(users) <= 100
        sizes.append(len(users))
        view = payload["view"]
        assert isinstance(view, Card)
        text = collected_text(view)
        assert len(text) <= 4000
        for user in users:
            assert f"<@{user.id}>" in text
    assert sizes == [100, 100, 50]
    assert progress == [(1, 3), (2, 3), (3, 3)]


async def test_ping_many_keeps_going_after_batch_failure() -> None:
    channel = _Channel(fail_on=3)
    progress: list[tuple[int, int]] = []

    async def mark(index: int, total: int) -> None:
        progress.append((index, total))

    report = await ping_many(
        SimpleNamespace(),
        channel,
        list(range(1, 251)),
        header=_header(),
        progress=mark,
    )
    assert report.batches == 3
    assert report.failed == 1
    assert report.sent == 2
    assert report.users == 150
    assert len(channel.payloads) == 3
    assert progress == [(1, 3), (2, 3), (3, 3)]
