import asyncio
from types import SimpleNamespace
from typing import Any, cast

from core.bot import Fishie


class _StatusPool:
    def __init__(self, most_used: str | None = None) -> None:
        self.most_used = most_used
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        if "ORDER BY COUNT(*)" in query:
            return self.most_used
        return 0


def test_rotating_status_renders_daily_most_used_command_name() -> None:
    pool = _StatusPool(most_used="fish help")
    bot = SimpleNamespace(
        pool=pool, logger=SimpleNamespace(exception=lambda *args: None)
    )

    status = asyncio.run(
        Fishie._format_rotating_status(
            cast(Any, bot), "Most used command today: {command_name}"
        )
    )

    assert status == "Most used command today: fish help"
    assert len(pool.calls) == 1
    assert "GROUP BY LOWER(BTRIM(command))" in pool.calls[0][0]


def test_rotating_status_skips_most_used_when_day_has_no_commands() -> None:
    pool = _StatusPool()
    bot = SimpleNamespace(
        pool=pool, logger=SimpleNamespace(exception=lambda *args: None)
    )

    status = asyncio.run(
        Fishie._format_rotating_status(
            cast(Any, bot), "Most used command today: {command_name}"
        )
    )

    assert status is None


def test_rotating_status_keeps_existing_command_totals() -> None:
    pool = _StatusPool()
    bot = SimpleNamespace(
        pool=pool, logger=SimpleNamespace(exception=lambda *args: None)
    )

    status = asyncio.run(
        Fishie._format_rotating_status(cast(Any, bot), "{commands_ran:,} commands ran")
    )

    assert status == "0 commands ran"
    assert len(pool.calls) == 2
