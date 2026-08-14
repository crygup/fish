from types import SimpleNamespace

from extensions.fun.connectfour import (
    COLOR_EMOJIS,
    COLS,
    ROWS,
    ConnectFourBoardView,
    ConnectFourGame,
    board_result,
    board_text,
    choose_ai_move,
    drop_piece,
    has_won,
    new_board,
    next_available_column,
)


def test_drop_piece_uses_gravity_and_detects_horizontal_win() -> None:
    board = new_board()
    for column in range(4):
        drop_piece(board, column, "yellow")
    assert board[ROWS - 1][:4] == ["yellow"] * 4
    assert has_won(board, "yellow")
    assert board_result(board) == "yellow"


def test_diagonal_win_and_full_board_result() -> None:
    board = new_board()
    board[5][0] = "red"
    board[4][1] = "red"
    board[3][2] = "red"
    board[2][3] = "red"
    assert has_won(board, "red")

    full = [
        ["yellow", "yellow", "yellow", "red", "yellow", "red", "yellow"],
        ["red", "yellow", "yellow", "red", "yellow", "red", "yellow"],
        ["red", "red", "red", "yellow", "yellow", "yellow", "red"],
        ["yellow", "yellow", "red", "red", "red", "yellow", "yellow"],
        ["red", "yellow", "yellow", "yellow", "red", "red", "red"],
        ["red", "yellow", "yellow", "red", "yellow", "yellow", "yellow"],
    ]
    assert board_result(full) == "draw"


def test_ai_returns_legal_moves_at_each_difficulty() -> None:
    board = new_board()
    for difficulty in ("easy", "normal", "hard"):
        assert choose_ai_move(board, "red", difficulty) in range(COLS)


def test_cursor_skips_full_columns_and_placement_uses_next_available_column() -> None:
    board = new_board()
    for _ in range(ROWS):
        drop_piece(board, 3, "yellow")
    assert next_available_column(board, 3, 1) == 4
    assert next_available_column(board, 3, -1) == 2


def test_cursor_occupies_and_can_fill_the_playable_top_row() -> None:
    controller = SimpleNamespace(bot=SimpleNamespace(embedcolor=0x123456))
    game = ConnectFourGame(
        controller=controller,
        ctx=None,
        players={"yellow": 1, "red": 2},
        names={1: "crygup", 2: "fishie"},
        against_bot=False,
        difficulty=None,
        guild_id=None,
        channel_id=1,
        started_by_id=1,
    )
    for row in range(1, ROWS):
        game.board[row][5] = "red"
    game.selected_column = 5
    lines = board_text(game).splitlines()
    assert len(lines) == ROWS
    assert lines[0].count(COLOR_EMOJIS["yellow"]) == 1
    assert drop_piece(game.board, 5, "yellow") == 0


def test_board_view_places_turn_summary_below_controls() -> None:
    controller = SimpleNamespace(bot=SimpleNamespace(embedcolor=0x123456))
    game = ConnectFourGame(
        controller=controller,
        ctx=None,
        players={"yellow": 1, "red": 2},
        names={1: "crygup", 2: "fishie"},
        against_bot=False,
        difficulty=None,
        guild_id=None,
        channel_id=1,
        started_by_id=1,
    )
    view = ConnectFourBoardView(game)
    assert view.footer.content == "🟡 crygup · 🔴 fishie · crygup's turn"
    assert [type(item).__name__ for item in view.container.children] == [
        "TextDisplay",
        "ActionRow",
        "Separator",
        "TextDisplay",
    ]
    assert [type(item).__name__ for item in view.children] == [
        "Container",
        "ActionRow",
    ]
    assert view.rematch.disabled
    game.result = "yellow"
    view.refresh()
    assert not view.rematch.disabled


def test_game_records_moves_with_player_and_board_coordinates() -> None:
    controller = SimpleNamespace(bot=SimpleNamespace(embedcolor=0x123456))
    game = ConnectFourGame(
        controller=controller,
        ctx=None,
        players={"yellow": 1, "red": 2},
        names={1: "crygup", 2: "fishie"},
        against_bot=False,
        difficulty=None,
        guild_id=None,
        channel_id=1,
        started_by_id=1,
    )
    row = drop_piece(game.board, 3, "yellow")
    game.record_move(color="yellow", player_id=1, column=3, row=row)
    assert game.move_count == 1
    assert game.move_history == [
        {"color": "yellow", "player_id": 1, "column": 3, "row": 5}
    ]
