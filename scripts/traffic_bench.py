"""Нагрузка регулятора против tests/fake_discord.py. Никогда не против discord.com."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.traffic_scenarios import block_live_base, format_table, run_all  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Стенд регулятора REST на FakeDiscord.")
    parser.add_argument(
        "--compressed",
        action="store_true",
        help="сокращённые сценарии, как в tests/test_traffic_bench.py",
    )
    return parser


async def _run(*, compressed: bool) -> int:
    block_live_base()
    rows = await run_all(compressed=compressed, log=lambda line: sys.stdout.write(line + "\n"))
    sys.stdout.write(format_table(rows) + "\n")
    return 0 if all(row.ok for row in rows) else 1


def main() -> None:
    args = _parser().parse_args()
    # До сценариев BASE ещё указывает на Discord — подменяем, чтобы ни один запрос не ушёл наружу.
    block_live_base()
    raise SystemExit(asyncio.run(_run(compressed=args.compressed)))


if __name__ == "__main__":
    main()
