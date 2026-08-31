from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import extensions.fun.tictactoe as tictactoe
from extensions.fun.tictactoe import (
    TicTacToeController,
    TicTacToeGame,
    board_result,
    choose_ai_move,
)


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


def test_completed_game_shows_awarded_coins() -> None:
    controller = TicTacToeController(
        SimpleNamespace(bot=SimpleNamespace(embedcolor=0x123456))
    )
    game = TicTacToeGame(
        controller=controller,
        ctx=None,
        players={"X": 1, "O": 2},
        names={1: "player", 2: "Fishie"},
        against_bot=True,
        difficulty="easy",
        guild_id=None,
        channel_id=10,
        started_by_id=1,
        result="X",
        coin_reward=10,
    )

    embed = controller.game_embed(game)

    assert embed.description is not None
    assert "player earned **10 Coins**." in embed.description


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
    controller = TicTacToeController(
        SimpleNamespace(bot=SimpleNamespace(pool=pool, user=SimpleNamespace(id=2)))
    )
    game = TicTacToeGame(
        controller=controller,
        ctx=None,
        players={"X": 1, "O": 2},
        names={1: "player", 2: "Fishie"},
        against_bot=True,
        difficulty=difficulty,
        guild_id=None,
        channel_id=10,
        started_by_id=1,
        result="X",
    )
    award = AsyncMock(return_value=amount)
    monkeypatch.setattr(tictactoe, "award_daily_capped_coins", award)

    assert await controller.award_bot_win(game) == amount
    award.assert_awaited_once_with(
        pool, 1, amount, daily_cap, f"game_tictactoe_{difficulty}"
    )


@pytest.mark.parametrize(
    ("result", "against_bot"),
    (("O", True), ("draw", True), ("X", False)),
)
async def test_bot_game_coins_require_a_human_win(
    monkeypatch: pytest.MonkeyPatch,
    result: str,
    against_bot: bool,
) -> None:
    controller = TicTacToeController(
        SimpleNamespace(bot=SimpleNamespace(pool=object(), user=SimpleNamespace(id=2)))
    )
    game = TicTacToeGame(
        controller=controller,
        ctx=None,
        players={"X": 1, "O": 2},
        names={1: "player", 2: "Fishie"},
        against_bot=against_bot,
        difficulty="easy" if against_bot else None,
        guild_id=None,
        channel_id=10,
        started_by_id=1,
        result=result,
    )
    award = AsyncMock()
    monkeypatch.setattr(tictactoe, "award_daily_capped_coins", award)

    assert await controller.award_bot_win(game) == 0
    award.assert_not_awaited()
