# Test doubles supply only the Discord/service fields exercised by each test.
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import discord
import pytest

import extensions.fun.connectfour as connectfour
from extensions.context import Context
from extensions.fun.connectfour import (
    COLOR_EMOJIS,
    COLS,
    ROWS,
    ConnectFourBoardView,
    ConnectFourController,
    ConnectFourGame,
    board_result,
    board_text,
    choose_ai_move,
    drop_piece,
    has_won,
    new_board,
    next_available_column,
    other_color,
    random_starting_color,
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

    full: list[list[str | None]] = [
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
        controller=cast("ConnectFourController", controller),
        ctx=cast("Context", None),
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
        controller=cast("ConnectFourController", controller),
        ctx=cast("Context", None),
        players={"yellow": 1, "red": 2},
        names={1: "crygup", 2: "fishie"},
        against_bot=False,
        difficulty=None,
        guild_id=None,
        channel_id=1,
        started_by_id=1,
    )
    view = ConnectFourBoardView(game)
    assert view.footer.content == "🔴 fishie · 🟡 crygup · crygup's turn"
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


def test_pvp_result_shows_the_winner_wager_pool() -> None:
    controller = SimpleNamespace(bot=SimpleNamespace(embedcolor=0x123456))
    game = ConnectFourGame(
        controller=cast("ConnectFourController", controller),
        ctx=cast("Context", None),
        players={"yellow": 1, "red": 2},
        names={1: "crygup", 2: "maronely"},
        against_bot=False,
        difficulty=None,
        guild_id=None,
        channel_id=1,
        started_by_id=1,
        result="yellow",
        pvp_payout=250,
    )
    view = ConnectFourBoardView(game)

    assert view.footer.content == "🔴 maronely · 🟡 crygup · crygup wins 250 Coins!"


def test_completed_game_shows_awarded_coins() -> None:
    controller = SimpleNamespace(bot=SimpleNamespace(embedcolor=0x123456))
    game = ConnectFourGame(
        controller=cast("ConnectFourController", controller),
        ctx=cast("Context", None),
        players={"yellow": 1, "red": 2},
        names={1: "player", 2: "Fishie"},
        against_bot=True,
        difficulty="normal",
        guild_id=None,
        channel_id=1,
        started_by_id=1,
        result="yellow",
        coin_reward=20,
    )

    view = ConnectFourBoardView(game)

    assert "Earned 20 Coins" in view.footer.content


def test_game_records_moves_with_player_and_board_coordinates() -> None:
    controller = SimpleNamespace(bot=SimpleNamespace(embedcolor=0x123456))
    game = ConnectFourGame(
        controller=cast("ConnectFourController", controller),
        ctx=cast("Context", None),
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


@pytest.mark.parametrize(
    ("difficulty", "amount", "daily_cap"),
    (("easy", 50, 10_000), ("normal", 200, 10_000), ("hard", 1_000, 10_000)),
)
async def test_bot_win_awards_difficulty_coins_with_a_shared_daily_cap(
    monkeypatch: pytest.MonkeyPatch,
    difficulty: str,
    amount: int,
    daily_cap: int,
) -> None:
    pool = object()
    controller = ConnectFourController(
        SimpleNamespace(bot=SimpleNamespace(pool=pool, user=SimpleNamespace(id=2)))
    )
    game = ConnectFourGame(
        controller=controller,
        ctx=cast("Context", None),
        players={"yellow": 1, "red": 2},
        names={1: "player", 2: "Fishie"},
        against_bot=True,
        difficulty=difficulty,
        guild_id=None,
        channel_id=10,
        started_by_id=1,
        result="yellow",
    )
    award = AsyncMock(return_value=amount)
    monkeypatch.setattr(connectfour, "award_daily_capped_coins", award)

    assert await controller.award_bot_win(game) == amount
    award.assert_awaited_once_with(
        pool, 1, amount, daily_cap, f"game_connectfour_{difficulty}"
    )


@pytest.mark.parametrize(
    ("result", "against_bot"),
    (("red", True), ("draw", True), ("yellow", False)),
)
async def test_bot_game_coins_require_a_human_win(
    monkeypatch: pytest.MonkeyPatch,
    result: str,
    against_bot: bool,
) -> None:
    controller = ConnectFourController(
        SimpleNamespace(bot=SimpleNamespace(pool=object(), user=SimpleNamespace(id=2)))
    )
    game = ConnectFourGame(
        controller=controller,
        ctx=cast("Context", None),
        players={"yellow": 1, "red": 2},
        names={1: "player", 2: "Fishie"},
        against_bot=against_bot,
        difficulty="easy" if against_bot else None,
        guild_id=None,
        channel_id=10,
        started_by_id=1,
        result=result,
    )
    award = AsyncMock()
    monkeypatch.setattr(connectfour, "award_daily_capped_coins", award)

    assert await controller.award_bot_win(game) == 0
    award.assert_not_awaited()


@pytest.mark.parametrize(
    ("random_bit", "expected"),
    ((0, "yellow"), (1, "red")),
)
def test_starting_color_uses_each_side_of_a_fair_random_bit(
    monkeypatch: pytest.MonkeyPatch, random_bit: int, expected: str
) -> None:
    monkeypatch.setattr(connectfour.random, "getrandbits", lambda _bits: random_bit)

    assert random_starting_color() == expected
    assert other_color(expected) != expected


async def test_random_player_can_start_as_red_and_records_its_opening_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=2, name="fishie"),
        embedcolor=0x123456,
    )
    controller = ConnectFourController(SimpleNamespace(bot=bot))
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1, name="player"),
        guild=None,
        channel=SimpleNamespace(id=10),
    )
    send_or_edit = AsyncMock()
    monkeypatch.setattr(controller, "_send_or_edit", send_or_edit)
    monkeypatch.setattr(controller, "schedule_timeout", lambda _game: None)
    # The random draw selects Fishie as player 1, so Fishie receives Red and
    # opens the game.
    monkeypatch.setattr(connectfour.random, "getrandbits", lambda _bits: 0)
    monkeypatch.setattr(connectfour, "choose_ai_move", lambda *_args: 3)
    monkeypatch.setattr(
        connectfour.asyncio,
        "to_thread",
        AsyncMock(side_effect=lambda function, *args: function(*args)),
    )

    game = await controller.start_bot_game(cast("Context", ctx), "hard")

    assert game is not None
    assert game.players == {"red": 2, "yellow": 1}
    assert game.human_color == "yellow"
    assert game.bot_color == "red"
    assert game.bot_id == 2
    assert game.current_color == "yellow"
    assert game.board[ROWS - 1][3] == "red"
    assert game.move_count == 1
    assert game.move_history == [
        {"color": "red", "player_id": 2, "column": 3, "row": ROWS - 1}
    ]
    assert controller.games == {1: game}
    send_or_edit.assert_awaited_once()


async def test_human_can_start_as_red_without_an_automatic_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=2, name="fishie"),
        embedcolor=0x123456,
    )
    controller = ConnectFourController(SimpleNamespace(bot=bot))
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1, name="player"),
        guild=None,
        channel=SimpleNamespace(id=10),
    )
    monkeypatch.setattr(controller, "_send_or_edit", AsyncMock())
    monkeypatch.setattr(controller, "schedule_timeout", lambda _game: None)
    monkeypatch.setattr(connectfour.random, "getrandbits", lambda _bits: 1)

    game = await controller.start_bot_game(cast("Context", ctx), "hard")

    assert game is not None
    assert game.players == {"red": 1, "yellow": 2}
    assert game.current_color == "red"
    assert game.move_count == 0
    assert game.move_history == []
    assert game.board == new_board()


async def test_other_bot_uses_its_name_but_keeps_fishie_stats_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=2, name="fishie"),
        embedcolor=0x123456,
    )
    controller = ConnectFourController(SimpleNamespace(bot=bot))
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1, name="player"),
        guild=None,
        channel=SimpleNamespace(id=10),
    )
    monkeypatch.setattr(controller, "_send_or_edit", AsyncMock())
    monkeypatch.setattr(controller, "schedule_timeout", lambda _game: None)
    # Let the human open so the test does not need to run the AI.
    monkeypatch.setattr(connectfour.random, "getrandbits", lambda _bits: 1)

    game = await controller.start_bot_game(
        cast("Context", ctx),
        "normal",
        opponent=cast("discord.User", SimpleNamespace(id=99, name="NotSoBot")),
    )

    assert game is not None
    assert game.players == {"red": 1, "yellow": 2}
    assert game.bot_id == 2
    assert game.names[game.bot_id] == "NotSoBot"
    assert "NotSoBot" in ConnectFourBoardView(game).footer.content


async def test_player_one_is_randomly_selected_for_player_duels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(embedcolor=0x123456)
    controller = ConnectFourController(SimpleNamespace(bot=bot))
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1, name="host"),
        guild=None,
        channel=SimpleNamespace(id=10),
    )
    opponent = SimpleNamespace(id=2, name="opponent")
    monkeypatch.setattr(controller, "_send_or_edit", AsyncMock())
    monkeypatch.setattr(controller, "schedule_timeout", lambda _game: None)
    # The second participant is selected as player 1 for this draw.
    monkeypatch.setattr(connectfour.random, "getrandbits", lambda _bits: 0)

    game = await controller.start_user_game(
        cast("Context", ctx), cast("discord.User", opponent)
    )

    assert game is not None
    assert game.players == {"red": 2, "yellow": 1}
    assert game.player_ids == (2, 1)
    assert game.current_color == "red"


async def test_bot_rematch_rerolls_colors_and_plays_when_fishie_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=2, name="fishie"),
        embedcolor=0x123456,
    )
    controller = ConnectFourController(SimpleNamespace(bot=bot))
    game = ConnectFourGame(
        controller=controller,
        ctx=cast("Context", None),
        players={"yellow": 1, "red": 2},
        names={1: "player", 2: "fishie"},
        against_bot=True,
        difficulty="hard",
        guild_id=None,
        channel_id=10,
        started_by_id=1,
        result="yellow",
        recorded=True,
    )
    game.view = ConnectFourBoardView(game)
    controller._register(game)
    monkeypatch.setattr(controller, "schedule_timeout", lambda _game: None)
    monkeypatch.setattr(connectfour.random, "getrandbits", lambda _bits: 0)
    monkeypatch.setattr(connectfour, "choose_ai_move", lambda *_args: 4)
    monkeypatch.setattr(
        connectfour.asyncio,
        "to_thread",
        AsyncMock(side_effect=lambda function, *args: function(*args)),
    )
    response = SimpleNamespace(
        is_done=lambda: False,
        defer=AsyncMock(),
        edit_message=AsyncMock(),
    )
    interaction = SimpleNamespace(response=response, message=SimpleNamespace())

    await controller.restart_game(
        game, cast("discord.Interaction[discord.Client]", interaction)
    )

    assert game.players == {"red": 2, "yellow": 1}
    assert game.current_color == "yellow"
    assert game.result is None
    assert game.move_history == [
        {"color": "red", "player_id": 2, "column": 4, "row": ROWS - 1}
    ]
    assert game.move_count == 1
    assert game.recorded is False
    assert controller.games == {1: game}
    response.edit_message.assert_awaited_once()


async def test_human_red_win_against_yellow_fishie_awards_coins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = object()
    controller = ConnectFourController(
        SimpleNamespace(bot=SimpleNamespace(pool=pool, user=SimpleNamespace(id=2)))
    )
    game = ConnectFourGame(
        controller=controller,
        ctx=cast("Context", None),
        players={"yellow": 2, "red": 1},
        names={1: "player", 2: "fishie"},
        against_bot=True,
        difficulty="hard",
        guild_id=None,
        channel_id=10,
        started_by_id=1,
        result="red",
    )
    award = AsyncMock(return_value=1_000)
    monkeypatch.setattr(connectfour, "award_daily_capped_coins", award)

    assert await controller.award_bot_win(game) == 1_000
    award.assert_awaited_once_with(pool, 1, 1_000, 10_000, "game_connectfour_hard")
