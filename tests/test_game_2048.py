from extensions.fun.game_2048 import (
    BOARD_SIZE,
    Game2048,
    empty_board,
    game_over,
    has_moves,
    move_board,
    slide_line,
)


def test_slide_line_merges_each_tile_once() -> None:
    assert slide_line([2, 2, 2, 2]) == ([4, 4, 0, 0], 8, True)
    assert slide_line([4, 4, 8, 8]) == ([8, 16, 0, 0], 24, True)


def test_move_board_supports_all_directions() -> None:
    board = empty_board()
    board[0][0] = 2
    board[0][1] = 2
    left, score, moved = move_board(board, "left")
    assert left[0] == [4, 0, 0, 0]
    assert score == 4
    assert moved

    down, _, moved = move_board(left, "down")
    assert down[-1][0] == 4
    assert moved

    right, _, moved = move_board(down, "right")
    assert right[-1][-1] == 4
    assert moved

    up, _, moved = move_board(right, "up")
    assert up[0][-1] == 4
    assert moved


def test_invalid_move_does_not_spawn_or_change_state() -> None:
    board = empty_board()
    board[0][0] = 2
    before = [row[:] for row in board]
    result, score, moved = move_board(board, "left")
    assert result == before
    assert score == 0
    assert not moved


def test_game_move_spawns_only_after_valid_move() -> None:
    game = Game2048(owner_id=1, board=empty_board())
    game.board[0][0] = 2
    game.board[0][1] = 2
    assert game.move("left")
    assert game.score == 4
    assert sum(value != 0 for row in game.board for value in row) == 2
    assert game.move_count == 1
    assert game.move_history == ["left"]


def test_no_double_merge_for_three_equal_tiles() -> None:
    board = empty_board()
    board[0] = [2, 2, 2, 0]
    result, score, _ = move_board(board, "left")
    assert result[0] == [4, 2, 0, 0]
    assert score == 4


def test_game_over_requires_full_board_without_merges() -> None:
    board = [
        [2, 4, 2, 4],
        [4, 2, 4, 2],
        [2, 4, 2, 4],
        [4, 2, 4, 2],
    ]
    assert not has_moves(board)
    assert game_over(board)


def test_empty_cell_or_merge_keeps_game_playable() -> None:
    board = [[2, 4, 8, 16] for _ in range(BOARD_SIZE)]
    board[0][0] = 0
    assert has_moves(board)
    assert not game_over(board)
