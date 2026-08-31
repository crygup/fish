"""Strategic regressions for Connect Four's deterministic hard-mode solver."""

from __future__ import annotations

from copy import deepcopy

import pytest

import extensions.fun.connectfour as connectfour
from extensions.fun.connectfour import (
    Board,
    board_result,
    choose_ai_move,
    drop_piece,
    has_won,
    legal_columns,
    new_board,
)
from extensions.fun.connectfour_solver import choose_best_move, score_moves


@pytest.fixture
def no_intentional_blunders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the public hard-mode wrapper on its normal best-move path."""

    monkeypatch.setattr(connectfour.random, "randrange", lambda _stop: 1)
    monkeypatch.setattr(connectfour.random, "choice", lambda choices: choices[0])


def _copy_after_drop(board: Board, column: int, color: str) -> Board:
    result = deepcopy(board)
    drop_piece(result, column, color)
    return result


def _immediate_wins(board: Board, color: str) -> list[int]:
    return [
        column
        for column in legal_columns(board)
        if has_won(_copy_after_drop(board, column, color), color)
    ]


def _opponent_fork_responses(
    board: Board, *, ai_column: int, ai_color: str, opponent_color: str
) -> list[int]:
    after_ai = _copy_after_drop(board, ai_column, ai_color)
    responses: list[int] = []
    for column in legal_columns(after_ai):
        after_opponent = _copy_after_drop(after_ai, column, opponent_color)
        if has_won(after_opponent, opponent_color):
            continue
        if len(_immediate_wins(after_opponent, opponent_color)) >= 2:
            responses.append(column)
    return responses


def test_hard_mode_takes_an_immediate_win(
    no_intentional_blunders: None,
) -> None:
    board = new_board()
    board[-1][:6] = ["red", "red", "red", "yellow", "yellow", "red"]
    # Column 3 is occupied at the bottom, so support a horizontal win one row up.
    board[-2][:3] = ["red", "red", "red"]

    assert board_result(board) is None
    assert choose_best_move(board, "red", time_limit=None) == 3
    assert choose_ai_move(board, "red", "hard") == 3


def test_hard_mode_blocks_an_immediate_loss(
    no_intentional_blunders: None,
) -> None:
    board = new_board()
    board[-1][:3] = ["yellow", "yellow", "yellow"]
    board[-1][4:] = ["red", "yellow", "red"]

    assert _immediate_wins(board, "yellow") == [3]
    assert choose_best_move(board, "red", time_limit=None) == 3
    assert choose_ai_move(board, "red", "hard") == 3


def test_hard_mode_search_does_not_mutate_the_board(
    no_intentional_blunders: None,
) -> None:
    board = new_board()
    for column, color in (
        (3, "yellow"),
        (2, "red"),
        (3, "yellow"),
        (4, "red"),
        (2, "yellow"),
        (4, "red"),
    ):
        drop_piece(board, column, color)
    original = deepcopy(board)

    score_moves(board, "red", max_depth=4, time_limit=None)
    assert board == original

    choose_ai_move(board, "red", "hard")
    assert board == original


def test_hard_mode_does_not_support_an_opponents_floating_threat() -> None:
    board = new_board()
    board[-1][:3] = ["red", "yellow", "red"]
    board[-2][:3] = ["yellow", "yellow", "yellow"]

    # Yellow's horizontal three is one row above an unsupported fourth square.
    # Red playing column 3 would supply that support and give Yellow an immediate
    # winning reply in the same column. The solver must value playability rather
    # than treating every geometrical three-in-a-row as equivalent.
    assert _immediate_wins(board, "yellow") == []
    poisoned = _copy_after_drop(board, 3, "red")
    assert _immediate_wins(poisoned, "yellow") == [3]

    moves = score_moves(board, "red", max_depth=4, time_limit=None)
    assert moves[0].column != 3
    assert next(move.score for move in moves if move.column == 3) < moves[0].score


def test_hard_mode_uses_the_only_defense_against_a_double_threat() -> None:
    board: Board = [
        [None, None, None, None, None, None, None],
        [None, None, None, None, None, "red", None],
        [None, None, None, None, None, "yellow", None],
        [None, None, None, None, None, "red", None],
        ["red", "yellow", None, "yellow", None, "red", "yellow"],
        ["red", "yellow", "yellow", "red", "red", "yellow", "yellow"],
    ]

    fork_responses = {
        column: _opponent_fork_responses(
            board,
            ai_column=column,
            ai_color="red",
            opponent_color="yellow",
        )
        for column in legal_columns(board)
    }
    safe_columns = [
        column for column, responses in fork_responses.items() if not responses
    ]

    assert safe_columns == [2]
    assert choose_best_move(board, "red", max_depth=4, time_limit=None) == 2


def test_supplied_endgame_was_already_an_unavoidable_double_threat() -> None:
    final_board: Board = [
        ["red", "yellow", "yellow", "yellow", "red", None, "yellow"],
        ["yellow", "red", "red", "red", "yellow", None, "red"],
        ["red", "yellow", "yellow", "yellow", "red", None, "yellow"],
        ["yellow", "red", "red", "yellow", "yellow", "yellow", "red"],
        ["red", "red", "yellow", "red", "yellow", "red", "yellow"],
        ["yellow", "red", "yellow", "yellow", "red", "red", "red"],
    ]
    assert board_result(final_board) == "yellow"

    # The winning piece is the topmost Yellow piece in column 5. One ply farther
    # back, remove a possible preceding Red move from column 0. Yellow then has
    # independently playable wins in columns 0 and 5, so Red has no saving move.
    before_yellow_win = deepcopy(final_board)
    before_yellow_win[3][5] = None
    assert _immediate_wins(before_yellow_win, "yellow") == [5]

    forced_loss = deepcopy(before_yellow_win)
    forced_loss[0][0] = None
    assert _immediate_wins(forced_loss, "yellow") == [0, 5]

    moves = score_moves(forced_loss, "red", time_limit=None)
    assert {move.column for move in moves} == {0, 5}
    assert all(move.score < -900_000 for move in moves)


def test_hard_search_uses_the_deep_winning_defense_from_the_reported_line() -> None:
    """The reported repeatable loss is avoidable before the final position."""

    # This is the position immediately before Fishie's eleventh move in the
    # supplied replay.  Columns in the report are one-based; the solver uses
    # zero-based columns.  The final board is not itself the bug—the loss was
    # already forced when Red chose column 3 instead of column 0 here.
    yellow_columns = (4, 4, 5, 3, 3, 5, 5, 5, 7, 5, 1)
    red_columns = (4, 4, 3, 6, 4, 5, 2, 6, 4, 7)
    board = new_board()
    for yellow_column, red_column in zip(yellow_columns, red_columns, strict=False):
        drop_piece(board, yellow_column - 1, "yellow")
        drop_piece(board, red_column - 1, "red")
    drop_piece(board, yellow_columns[-1] - 1, "yellow")

    moves = score_moves(board, "red", max_depth=21, time_limit=None)

    assert moves[0].column == 0
    assert next(move.score for move in moves if move.column == 2) < -900_000
