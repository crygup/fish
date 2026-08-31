from copy import deepcopy

import pytest

from extensions.fun.connectfour import drop_piece, new_board
from extensions.fun.connectfour_solver import (
    WIN_SCORE,
    choose_best_move,
    score_moves,
)


def test_solver_takes_an_immediate_win_for_either_color() -> None:
    for color in ("yellow", "red"):
        board = new_board()
        for column in range(3):
            drop_piece(board, column, color)
        assert choose_best_move(board, color, max_depth=5, time_limit=None) == 3
        assert score_moves(board, color, max_depth=5, time_limit=None)[0].score == (
            WIN_SCORE - 1
        )


def test_solver_blocks_the_opponents_only_immediate_win() -> None:
    board = new_board()
    for column in range(3):
        drop_piece(board, column, "yellow")
    drop_piece(board, 4, "red")

    scores = score_moves(board, "red", max_depth=6, time_limit=None)

    assert scores[0].column == 3
    assert scores[0].tactical == "block"
    assert all(move.score <= -WIN_SCORE + 2 for move in scores if move.column != 3)


def test_solver_prefers_the_center_on_an_empty_board_deterministically() -> None:
    board = new_board()
    choices = {
        choose_best_move(board, "yellow", max_depth=5, time_limit=None)
        for _ in range(3)
    }
    assert choices == {3}


def test_solver_recognizes_an_unavoidable_double_threat() -> None:
    board = new_board()
    for column in (1, 2, 3):
        drop_piece(board, column, "yellow")

    scores = score_moves(board, "red", max_depth=5, time_limit=None)

    # Yellow can win at either end, so every red continuation is a forced loss.
    assert {move.column for move in scores} == set(range(7))
    assert all(move.score <= -WIN_SCORE + 2 for move in scores)
    assert all(move.tactical == "forced_loss" for move in scores)


def test_solver_avoids_giving_the_opponent_a_supported_win() -> None:
    board = new_board()
    # Yellow has a floating horizontal three on row 4. Playing column 3 as red
    # would support yellow's winning square; a gravity-aware solver avoids it.
    for column, support in zip((0, 1, 2), ("red", "yellow", "red"), strict=True):
        drop_piece(board, column, support)
        drop_piece(board, column, "yellow")

    assert choose_best_move(board, "red", max_depth=6, time_limit=None) != 3


def test_solver_does_not_mutate_the_input_board() -> None:
    board = new_board()
    for column, color in ((3, "yellow"), (2, "red"), (3, "yellow")):
        drop_piece(board, column, color)
    original = deepcopy(board)

    score_moves(board, "red", max_depth=6, time_limit=None)

    assert board == original


def test_solver_rejects_unsupported_or_finished_positions() -> None:
    unsupported = new_board()
    unsupported[0][0] = "yellow"
    with pytest.raises(ValueError, match="gravity"):
        score_moves(unsupported, "red")

    finished = new_board()
    for column in range(4):
        drop_piece(finished, column, "yellow")
    with pytest.raises(ValueError, match="already ended"):
        score_moves(finished, "red")
