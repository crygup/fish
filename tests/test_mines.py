from __future__ import annotations

import random

import discord
import pytest

from extensions.fun.mines import (
    CELL_COUNT,
    MAX_BOMBS,
    MIN_BOMBS,
    MinesGame,
    MinesView,
)
from extensions.fun.tictactoe import EMPTY_CELL_LABEL


def test_mines_board_has_twenty_five_tiles_and_valid_mine_count() -> None:
    game = MinesGame.new(1, 100, 7, 5, rng=random.Random(4))

    assert len(game.mines) == 5
    assert len(game.mines | set(range(CELL_COUNT))) == CELL_COUNT
    assert MIN_BOMBS <= game.bomb_count <= MAX_BOMBS


@pytest.mark.parametrize(
    ("bombs", "expected"),
    ((1, 1.0104), (3, 1.1023), (5, 1.2125), (10, 1.6167), (15, 2.425)),
)
def test_first_safe_pick_uses_survival_probability_and_house_edge(
    bombs: int, expected: float
) -> None:
    game = MinesGame.new(1, 100, 7, bombs, rng=random.Random(1))
    safe = next(index for index in range(CELL_COUNT) if index not in game.mines)

    assert game.reveal(safe) == "safe"
    assert game.multiplier == pytest.approx(expected, abs=0.001)


def test_mine_ends_game_without_a_payout() -> None:
    game = MinesGame.new(1, 100, 7, 3, rng=random.Random(3))
    mine = next(iter(game.mines))

    assert game.reveal(mine) == "mine"
    assert game.finished
    assert game.payout == 0


def test_cash_out_requires_a_safe_pick_and_ends_game() -> None:
    game = MinesGame.new(1, 100, 7, 3, rng=random.Random(3))

    with pytest.raises(ValueError):
        game.cash_out()
    safe = next(index for index in range(CELL_COUNT) if index not in game.mines)
    game.reveal(safe)
    game.cash_out()

    assert game.finished and game.cashed_out
    assert game.payout == 110


def test_mines_view_is_a_components_v2_five_by_five_board() -> None:
    game = MinesGame.new(1, 100, 7, 3, rng=random.Random(2))
    view = MinesView(game)
    rows = [
        item
        for item in view.container.children
        if isinstance(item, discord.ui.ActionRow)
    ]

    assert isinstance(view, discord.ui.LayoutView)
    assert [len(row.children) for row in rows] == [5, 5, 5, 5, 5]
    assert all(button.label == EMPTY_CELL_LABEL for button in view.buttons)
    assert view.cash_out_button.disabled


def test_revealed_tile_drops_the_blank_label() -> None:
    game = MinesGame.new(1, 100, 7, 3, rng=random.Random(2))
    view = MinesView(game)
    safe = next(index for index in range(CELL_COUNT) if index not in game.mines)

    game.reveal(safe)
    view.refresh()

    assert view.buttons[safe].label is None
    assert view.buttons[safe].emoji is not None
    assert view.buttons[safe].emoji.name == "💎"
