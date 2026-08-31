from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import extensions.fun.wordbomb_stats as wordbomb_stats


@pytest.mark.asyncio
async def test_record_wordbomb_result_tracks_a_win_and_loss() -> None:
    pool = AsyncMock()

    await wordbomb_stats.record_wordbomb_result(pool, 42, won=True)
    await wordbomb_stats.record_wordbomb_result(pool, 42, won=False)

    assert pool.execute.await_count == 2
    first = pool.execute.await_args_list[0].args
    assert first[1:] == (42, 1, 0)
    second = pool.execute.await_args_list[1].args
    assert second[1:] == (42, 0, 1)


@pytest.mark.asyncio
async def test_record_wordbomb_result_honors_game_tracking_opt_out() -> None:
    pool = AsyncMock()

    await wordbomb_stats.record_wordbomb_result(
        pool, 42, won=False, tracking_enabled=False
    )

    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_wordbomb_winner_reward_uses_daily_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    award = AsyncMock(return_value=100)
    monkeypatch.setattr(wordbomb_stats, "award_daily_capped_coins", award)
    pool = object()

    result = await wordbomb_stats.award_wordbomb_winner_coins(pool, 42)

    assert result == 100
    award.assert_awaited_once_with(pool, 42, 100, 5_000, "wordbomb")
