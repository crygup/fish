from extensions.fun.memory import MemoryGame, shuffled_pairs


def test_memory_board_contains_eight_pairs() -> None:
    board = shuffled_pairs()
    assert len(board) == 16
    assert sorted(board.count(value) for value in set(board)) == [2] * 8


def test_memory_matching_and_mismatch_lifecycle() -> None:
    board = (
        "a",
        "b",
        "a",
        "b",
        "c",
        "c",
        "d",
        "d",
        "e",
        "e",
        "f",
        "f",
        "g",
        "g",
        "h",
        "h",
    )
    game = MemoryGame(user_id=1, board=board)
    assert game.select(0) == "first"
    assert game.select(2) == "match"
    assert {0, 2} <= game.matched
    assert game.select(1) == "first"
    assert game.select(4) == "second"
    assert game.resolving
    game.hide_unmatched(1, 4)
    assert not game.resolving
    assert 1 not in game.revealed and 4 not in game.revealed


def test_memory_rejects_completed_tiles_and_same_tile() -> None:
    game = MemoryGame(
        user_id=1,
        board=(
            "a",
            "a",
            "b",
            "b",
            "c",
            "c",
            "d",
            "d",
            "e",
            "e",
            "f",
            "f",
            "g",
            "g",
            "h",
            "h",
        ),
    )
    assert game.select(0) == "first"
    assert game.select(0) == "ignored"
    assert game.select(1) == "match"
    assert game.select(0) == "ignored"
