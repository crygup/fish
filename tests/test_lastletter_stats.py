from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import extensions.fun.lastletter_stats as lastletter_stats


@pytest.mark.asyncio
async def test_record_lastletter_result_tracks_a_win_and_loss() -> None:
    pool = AsyncMock()

    await lastletter_stats.record_lastletter_result(pool, 42, won=True)
    await lastletter_stats.record_lastletter_result(pool, 42, won=False)

    assert pool.execute.await_count == 2
    assert pool.execute.await_args_list[0].args[1:] == (42, 1, 0)
    assert pool.execute.await_args_list[1].args[1:] == (42, 0, 1)


@pytest.mark.asyncio
async def test_record_lastletter_result_honors_tracking_opt_out() -> None:
    pool = AsyncMock()

    await lastletter_stats.record_lastletter_result(
        pool, 42, won=False, tracking_enabled=False
    )

    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_lastletter_winner_reward_uses_daily_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    award = AsyncMock(return_value=5_000)
    monkeypatch.setattr(lastletter_stats, "award_daily_capped_coins", award)
    pool = object()

    result = await lastletter_stats.award_lastletter_winner_coins(pool, 42, 9_000)

    assert result == 5_000
    award.assert_awaited_once_with(
        pool,
        42,
        9_000,
        lastletter_stats.LASTLETTER_DAILY_REWARD_CAP,
        "lastletter",
    )
