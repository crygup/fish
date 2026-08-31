import random
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

import extensions.fun as fun_module
from extensions.fun import Fun
from extensions.fun.lightsout import (
    CELL_COUNT,
    OFF_BOARD,
    LightsOutGame,
    LightsOutView,
    affected_indices,
    completion_payout,
    is_solved,
    press_board,
    scramble_board,
)
from extensions.fun.tictactoe import EMPTY_CELL_LABEL


def test_corner_edge_and_center_affect_only_orthogonal_neighbors() -> None:
    assert set(affected_indices(0)) == {0, 1, 5}
    assert set(affected_indices(2)) == {1, 2, 3, 7}
    assert set(affected_indices(12)) == {7, 11, 12, 13, 17}


def test_pressing_the_same_light_twice_restores_the_board() -> None:
    board = press_board(OFF_BOARD, 12)
    assert press_board(board, 12) == OFF_BOARD


def test_invalid_light_indices_are_rejected() -> None:
    with pytest.raises(IndexError):
        press_board(OFF_BOARD, -1)
    with pytest.raises(IndexError):
        press_board(OFF_BOARD, CELL_COUNT)


def test_generated_boards_are_nonempty_and_solvable() -> None:
    for seed in range(50):
        board, scramble = scramble_board(random.Random(seed))
        assert not is_solved(board)
        for index in reversed(scramble):
            board = press_board(board, index)
        assert is_solved(board)


def test_game_counts_moves_and_stops_after_being_solved() -> None:
    game = LightsOutGame(user_id=1, board=press_board(OFF_BOARD, 12))
    assert game.press(12)
    assert game.finished
    assert game.move_count == 1
    assert not game.press(0)
    assert game.move_count == 1


@pytest.mark.parametrize(
    ("duration", "expected"),
    ((0, 1000), (300, 505), (600, 10), (601, 10), (3600, 10)),
)
def test_completion_payout_scales_to_a_ten_minute_floor(
    duration: float, expected: int
) -> None:
    assert completion_payout(duration) == expected


def test_view_builds_five_rows_of_five_buttons() -> None:
    game = LightsOutGame(user_id=1, board=press_board(OFF_BOARD, 12))
    view = LightsOutView(game)
    rows = [
        item
        for item in view.container.children
        if isinstance(item, discord.ui.ActionRow)
    ]
    assert len(rows) == 5
    assert [len(row.children) for row in rows] == [5, 5, 5, 5, 5]
    assert len(view.buttons) == CELL_COUNT
    assert all(button.label == EMPTY_CELL_LABEL for button in view.buttons)
    assert (
        sum(button.style is discord.ButtonStyle.success for button in view.buttons) == 5
    )
    assert (
        sum(button.style is discord.ButtonStyle.secondary for button in view.buttons)
        == 20
    )


def test_finished_view_disables_every_light() -> None:
    game = LightsOutGame(user_id=1, board=press_board(OFF_BOARD, 12))
    view = LightsOutView(game)
    assert game.press(12)
    view.refresh()
    assert all(button.disabled for button in view.buttons)
    assert all(button.style is discord.ButtonStyle.secondary for button in view.buttons)
    assert "Puzzle complete" in view.status.content


async def test_only_the_game_author_can_interact() -> None:
    game = LightsOutGame(user_id=1, board=press_board(OFF_BOARD, 12))
    view = LightsOutView(game)
    owner = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock()),
        ),
    )
    outsider = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=2),
            response=SimpleNamespace(send_message=AsyncMock()),
        ),
    )

    assert await view.interaction_check(owner)
    assert not await view.interaction_check(outsider)
    outsider.response.send_message.assert_awaited_once()
    assert outsider.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_timeout_disables_the_board_and_runs_cleanup_once() -> None:
    game = LightsOutGame(user_id=1, board=press_board(OFF_BOARD, 12))
    on_finish = AsyncMock()
    view = LightsOutView(game, on_finish=on_finish)

    await view.on_timeout()

    assert game.finished and game.timed_out
    assert all(button.disabled for button in view.buttons)
    on_finish.assert_awaited_once_with(game, True)


async def test_solving_lights_out_awards_capped_daily_coins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = LightsOutGame(
        user_id=1,
        board=OFF_BOARD,
        finished=True,
        completed_duration=0,
    )
    pool = SimpleNamespace(execute=AsyncMock())
    cog = SimpleNamespace(
        _lightsout_games={1: game},
        bot=SimpleNamespace(
            pool=pool,
            db_cache=SimpleNamespace(user_game_tracking_enabled=lambda _user_id: False),
            logger=SimpleNamespace(exception=AsyncMock()),
        ),
    )
    award = AsyncMock(return_value=10)
    monkeypatch.setattr(fun_module, "award_daily_capped_coins", award)

    await Fun._finish_lightsout(cast(Any, cog), game, False)

    award.assert_awaited_once_with(pool, 1, 1000, 5000, "lightsout")
    pool.execute.assert_not_awaited()


@pytest.mark.parametrize("timed_out", (True, False))
async def test_incomplete_lights_out_does_not_award_coins(
    monkeypatch: pytest.MonkeyPatch,
    timed_out: bool,
) -> None:
    game = LightsOutGame(user_id=1, board=press_board(OFF_BOARD, 12))
    pool = SimpleNamespace(execute=AsyncMock())
    cog = SimpleNamespace(
        _lightsout_games={1: game},
        bot=SimpleNamespace(
            pool=pool,
            db_cache=SimpleNamespace(user_game_tracking_enabled=lambda _user_id: True),
            logger=SimpleNamespace(exception=AsyncMock()),
        ),
    )
    award = AsyncMock()
    monkeypatch.setattr(fun_module, "award_daily_capped_coins", award)

    await Fun._finish_lightsout(cast(Any, cog), game, timed_out)

    award.assert_not_awaited()
    pool.execute.assert_not_awaited()
