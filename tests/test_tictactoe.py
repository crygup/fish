from extensions.fun.tictactoe import board_result, choose_ai_move


def test_board_result_detects_wins_and_draws() -> None:
    assert board_result(["X", "X", "X", None, None, None, None, None, None]) == "X"
    assert board_result(["O", None, None, "O", "X", None, "O", "X", "X"]) == "O"
    assert board_result(["X", "O", "X", "X", "O", "O", "O", "X", "X"]) == "draw"


def test_hard_ai_takes_a_winning_move() -> None:
    board = ["O", "O", None, "X", "X", None, None, None, None]
    assert choose_ai_move(board, "O", "hard") == 2


def test_easy_ai_can_choose_a_worse_move() -> None:
    board = [None, None, None, None, None, "X", "X", "O", "O"]
    assert choose_ai_move(board, "O", "easy") != 4
