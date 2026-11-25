from __future__ import annotations

import pytest
from tests.traffic_scenarios import refuse_if_live, run_all


def test_bench_refuses_live_discord() -> None:
    with pytest.raises(SystemExit, match="Discord"):
        refuse_if_live("https://discord.com/api/v10")
    with pytest.raises(SystemExit, match="Discord"):
        refuse_if_live("https://discordapp.com/api")


async def test_compressed_scenarios_pass() -> None:
    rows = await run_all(compressed=True)
    failed = [row.scenario for row in rows if not row.ok]
    assert failed == [], {row.scenario: row.actual for row in rows if not row.ok}
