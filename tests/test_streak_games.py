from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from core.currency import InsufficientFunds, InvalidAmount, Wager, WagerSettlement
from extensions.fun import Fun
from extensions.fun.streak_games import (
    HeadsOrTailsGame,
    HeadsOrTailsView,
    HigherOrLowerGame,
    HigherOrLowerView,
    PlayingCard,
    RockPaperScissorsGame,
    RockPaperScissorsWagerView,
    RPSChoice,
    quarter_size_cards,
)


class FixedChoice:
    def __init__(self, values: list[object]) -> None:
        self.values = iter(values)

    def choice(self, _items):
        return next(self.values)


def test_quarter_size_card_deck_contains_every_card() -> None:
    cards = quarter_size_cards()
    assert len(cards) == 52
    assert {card.suit for card in cards} == {"clubs", "diamonds", "hearts", "spades"}
    assert {card.value for card in cards} == set(range(2, 15))
    assert all(card.path.parent.name == "25" for card in cards)


def test_higher_or_lower_continues_until_a_wrong_guess() -> None:
    two = PlayingCard(quarter_size_cards()[0].path, "clubs", "2", 2)
    ten = PlayingCard(quarter_size_cards()[1].path, "hearts", "10", 10)
    king = PlayingCard(quarter_size_cards()[2].path, "spades", "king", 13)
    rng = FixedChoice([two, ten, king])
    game = HigherOrLowerGame(1, deck=(two, ten, king), rng=rng)  # type: ignore[arg-type]

    assert game.guess("higher") is True
    assert game.streak == 1
    assert game.finished is False
    assert game.guess("lower") is False
    assert game.streak == 1
    assert game.finished is True


def test_higher_or_lower_never_reveals_the_same_rank_twice() -> None:
    two_clubs = PlayingCard(quarter_size_cards()[0].path, "clubs", "2", 2)
    two_hearts = PlayingCard(quarter_size_cards()[13].path, "hearts", "2", 2)
    ten = PlayingCard(quarter_size_cards()[1].path, "hearts", "10", 10)
    # This RNG deliberately returns an invalid same-rank card first.  The
    # game must still enforce the invariant rather than trusting the RNG.
    rng = FixedChoice([two_clubs, two_hearts])
    game = HigherOrLowerGame(
        1,
        deck=(two_clubs, two_hearts, ten),
        rng=rng,  # type: ignore[arg-type]
    )

    game.guess("higher")

    assert game.revealed_card is not None
    assert game.revealed_card.value != 2


def test_higher_or_lower_wager_compounds_a_constant_1_2_multiplier() -> None:
    two = PlayingCard(quarter_size_cards()[0].path, "clubs", "2", 2)
    five = PlayingCard(quarter_size_cards()[1].path, "hearts", "5", 5)
    ten = PlayingCard(quarter_size_cards()[2].path, "spades", "10", 10)
    king = PlayingCard(quarter_size_cards()[3].path, "diamonds", "king", 13)
    rng = FixedChoice([two, five, ten, king])
    game = HigherOrLowerGame(
        1,
        deck=(two, five, ten, king),
        rng=rng,  # type: ignore[arg-type]
        wager_id=10,
        stake=50,
    )

    assert game.potential_payout == 50
    assert game.guess("higher") is True
    assert game.potential_payout == 60
    assert game.guess("higher") is True
    assert game.potential_payout == 72
    assert game.guess("higher") is True
    assert game.potential_payout == 86


def test_higher_or_lower_wager_caps_without_ending_the_streak() -> None:
    two = PlayingCard(quarter_size_cards()[0].path, "clubs", "2", 2)
    five = PlayingCard(quarter_size_cards()[1].path, "hearts", "5", 5)
    ten = PlayingCard(quarter_size_cards()[2].path, "spades", "10", 10)
    rng = FixedChoice([two, five, ten])
    game = HigherOrLowerGame(
        1,
        deck=(two, five, ten),
        rng=rng,  # type: ignore[arg-type]
        wager_id=10,
        stake=5,
        payout_limit=6,
    )

    assert game.guess("higher") is True
    assert game.potential_payout == 6
    assert game.guess("higher") is True
    assert game.potential_payout == 6
    assert game.payout_capped
    assert not game.finished


def test_heads_or_tails_tracks_an_endless_streak_until_failure() -> None:
    rng = FixedChoice(["heads", "heads", "tails"])
    game = HeadsOrTailsGame(1, rng=rng)  # type: ignore[arg-type]
    assert game.guess("heads") is True
    assert game.guess("heads") is True
    assert game.streak == 2
    assert game.guess("heads") is False
    assert game.finished is True


def test_streak_games_use_components_v2_and_neutral_buttons() -> None:
    card_view = HigherOrLowerView(
        HigherOrLowerGame(1, rng=random.Random(1)), accent_color=discord.Colour.blue()
    )
    coin_view = HeadsOrTailsView(
        HeadsOrTailsGame(1, rng=random.Random(1)), accent_color=discord.Colour.blue()
    )
    assert isinstance(card_view, discord.ui.LayoutView)
    assert isinstance(coin_view, discord.ui.LayoutView)
    assert card_view.higher.style is discord.ButtonStyle.secondary
    assert card_view.lower.style is discord.ButtonStyle.secondary
    assert coin_view.heads.style is discord.ButtonStyle.secondary
    assert coin_view.tails.style is discord.ButtonStyle.secondary
    assert card_view.cash_out not in card_view.container.walk_children()


def test_wagered_higher_or_lower_view_has_cash_out_button() -> None:
    game = HigherOrLowerGame(
        1, rng=random.Random(1), wager_id=2, stake=10, payout_limit=1000
    )
    view = HigherOrLowerView(game)

    assert view.cash_out in view.container.walk_children()
    assert view.cash_out.style is discord.ButtonStyle.success
    assert view.cash_out.disabled
    assert "Cash-out value:** 10 Coins" in view.display.content


async def test_higher_or_lower_cash_out_is_only_settled_once() -> None:
    game = HigherOrLowerGame(
        1, rng=random.Random(1), wager_id=2, stake=10, payout_limit=1000
    )
    game.streak = 1
    settle = AsyncMock(return_value=True)
    finish = AsyncMock()
    view = HigherOrLowerView(game, on_cash_out=settle, on_finish=finish)
    first = cast(
        Any,
        SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        ),
    )
    second = cast(
        Any,
        SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        ),
    )

    await view.cash_out.callback(first)
    await view.cash_out.callback(second)

    settle.assert_awaited_once_with(game)
    finish.assert_awaited_once_with(game, False)
    assert game.finished and game.cashed_out
    assert "Cashed out **10 Coins**" in view.display.content
    first.response.edit_message.assert_awaited_once()
    second.response.send_message.assert_awaited_once_with(
        "This Higher or Lower game is over.", ephemeral=True
    )


async def test_higher_or_lower_cannot_cash_out_before_a_correct_guess() -> None:
    game = HigherOrLowerGame(
        1, rng=random.Random(1), wager_id=2, stake=10, payout_limit=1000
    )
    settle = AsyncMock(return_value=True)
    view = HigherOrLowerView(game, on_cash_out=settle)
    interaction = cast(
        Any,
        SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        ),
    )

    await view.cash_out.callback(interaction)

    settle.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert (
        "at least one correct guess"
        in interaction.response.send_message.await_args.args[0]
    )


def higher_or_lower_cog(currency: Any) -> Any:
    return SimpleNamespace(
        bot=SimpleNamespace(
            currency=currency,
            embedcolor=discord.Colour.blurple(),
            logger=Mock(),
        ),
        _higher_or_lower_games={},
        _finish_higher_or_lower=AsyncMock(),
        _record_higher_or_lower_progress=AsyncMock(),
        _cash_out_higher_or_lower=AsyncMock(return_value=True),
    )


async def test_starting_wager_reserves_coins_before_showing_game() -> None:
    currency = SimpleNamespace(
        open_wager=AsyncMock(return_value=Wager(7, 1, "higher_lower", 25, "open", 0)),
        settle_wager=AsyncMock(),
    )
    cog = higher_or_lower_cog(currency)
    message = SimpleNamespace()
    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=1), send=AsyncMock(return_value=message)
        ),
    )

    await Fun._start_higher_or_lower(cog, ctx, 25)

    currency.open_wager.assert_awaited_once_with(1, 25, source="higher_lower")
    game = cog._higher_or_lower_games[1]
    assert game.wager_id == 7
    assert game.stake == game.potential_payout == 25
    assert isinstance(game.view, HigherOrLowerView)
    assert game.view.message is message


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidAmount(), "at least **10 Coins**"),
        (InsufficientFunds(0, 25), "wallet is empty"),
        (InsufficientFunds(10, 25), "only have **10 Coins**"),
    ],
)
async def test_invalid_wagers_are_explained_without_starting_game(
    error: Exception, expected: str
) -> None:
    currency = SimpleNamespace(open_wager=AsyncMock(side_effect=error))
    cog = higher_or_lower_cog(currency)
    ctx = cast(
        Any,
        SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock()),
    )

    await Fun._start_higher_or_lower(cog, ctx, 25)

    assert not cog._higher_or_lower_games
    assert expected in ctx.send.await_args.args[0]


async def test_failed_game_message_refunds_reserved_wager() -> None:
    wager = Wager(7, 1, "higher_lower", 25, "open", 0)
    currency = SimpleNamespace(
        open_wager=AsyncMock(return_value=wager),
        settle_wager=AsyncMock(),
    )
    cog = higher_or_lower_cog(currency)
    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            send=AsyncMock(side_effect=RuntimeError("Discord unavailable")),
        ),
    )

    with pytest.raises(RuntimeError, match="Discord unavailable"):
        await Fun._start_higher_or_lower(cog, ctx, 25)

    currency.settle_wager.assert_awaited_once_with(wager.id, wager.stake)
    assert not cog._higher_or_lower_games


async def test_cash_out_uses_the_idempotently_stored_payout() -> None:
    stored = Wager(7, 1, "higher_lower", 10, "cashed_out", 33)
    currency = SimpleNamespace(
        settle_wager=AsyncMock(
            return_value=WagerSettlement(stored, settled=False, balance=100)
        )
    )
    cog = higher_or_lower_cog(currency)
    game = HigherOrLowerGame(
        1, rng=random.Random(1), wager_id=7, stake=10, payout_limit=1000
    )
    game.potential_payout = 40

    assert await Fun._cash_out_higher_or_lower(cog, game)
    assert game.potential_payout == 33
    currency.settle_wager.assert_awaited_once_with(7, 40)


@pytest.mark.parametrize("timed_out", [False, True])
async def test_wrong_or_timed_out_wager_forfeits_payout(timed_out: bool) -> None:
    currency = SimpleNamespace(settle_wager=AsyncMock())
    cog = higher_or_lower_cog(currency)
    game = HigherOrLowerGame(
        1, rng=random.Random(1), wager_id=7, stake=10, payout_limit=1000
    )
    game.finished = True
    game.timed_out = timed_out
    game.potential_payout = 50
    cog._higher_or_lower_games[1] = game

    await Fun._finish_higher_or_lower(cog, game, timed_out)

    currency.settle_wager.assert_awaited_once_with(7, 0)
    assert 1 not in cog._higher_or_lower_games


def test_heads_or_tails_wager_compounds_each_multiplier() -> None:
    game = HeadsOrTailsGame(
        1,
        rng=FixedChoice(["heads", "heads", "heads"]),  # type: ignore[arg-type]
        wager_id=10,
        stake=5,
    )

    assert game.potential_payout == 5
    assert game.guess("heads") is True
    assert game.potential_payout == 5
    assert game.guess("heads") is True
    assert game.potential_payout == 6
    assert game.guess("heads") is True
    assert game.potential_payout == 7


def test_wagered_heads_or_tails_has_cash_out_without_restart() -> None:
    game = HeadsOrTailsGame(
        1, rng=random.Random(1), wager_id=2, stake=10, payout_limit=1000
    )
    view = HeadsOrTailsView(game)

    assert view.cash_out in view.container.walk_children()
    assert view.cash_out.style is discord.ButtonStyle.success
    assert view.restart not in view.walk_children()
    assert "Cash-out value:** 10 Coins" in view.display.content


async def test_heads_or_tails_cash_out_is_only_settled_once() -> None:
    game = HeadsOrTailsGame(
        1, rng=random.Random(1), wager_id=2, stake=10, payout_limit=1000
    )
    settle = AsyncMock(return_value=True)
    finish = AsyncMock()
    view = HeadsOrTailsView(game, on_cash_out=settle, on_finish=finish)
    first = cast(
        Any,
        SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        ),
    )
    second = cast(
        Any,
        SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        ),
    )

    await view.cash_out.callback(first)
    await view.cash_out.callback(second)

    settle.assert_awaited_once_with(game)
    finish.assert_awaited_once_with(game, False)
    assert game.finished and game.cashed_out
    assert "Cashed out **10 Coins**" in view.display.content
    second.response.send_message.assert_awaited_once_with(
        "This Heads or Tails game is over.", ephemeral=True
    )


def heads_or_tails_cog(currency: Any) -> Any:
    return SimpleNamespace(
        bot=SimpleNamespace(
            currency=currency,
            embedcolor=discord.Colour.blurple(),
            logger=Mock(),
        ),
        _heads_or_tails_games={},
        _finish_heads_or_tails=AsyncMock(),
        _record_heads_or_tails_progress=AsyncMock(),
        _restart_heads_or_tails=AsyncMock(return_value=True),
        _cash_out_heads_or_tails=AsyncMock(return_value=True),
    )


async def test_starting_heads_or_tails_wager_reserves_coins() -> None:
    currency = SimpleNamespace(
        open_wager=AsyncMock(return_value=Wager(8, 1, "heads_tails", 25, "open", 0)),
        settle_wager=AsyncMock(),
    )
    cog = heads_or_tails_cog(currency)
    message = SimpleNamespace()
    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=1), send=AsyncMock(return_value=message)
        ),
    )

    await Fun._start_heads_or_tails(cog, ctx, 25)

    currency.open_wager.assert_awaited_once_with(1, 25, source="heads_tails")
    game = cog._heads_or_tails_games[1]
    assert game.wager_id == 8
    assert game.stake == game.potential_payout == 25
    assert isinstance(game.view, HeadsOrTailsView)
    assert game.view.message is message


async def test_heads_or_tails_cash_out_uses_stored_payout() -> None:
    stored = Wager(8, 1, "heads_tails", 10, "cashed_out", 33)
    currency = SimpleNamespace(
        settle_wager=AsyncMock(
            return_value=WagerSettlement(stored, settled=True, balance=100)
        )
    )
    cog = heads_or_tails_cog(currency)
    game = HeadsOrTailsGame(
        1, rng=random.Random(1), wager_id=8, stake=10, payout_limit=1000
    )
    game.potential_payout = 40

    assert await Fun._cash_out_heads_or_tails(cog, game)
    assert game.potential_payout == 33
    currency.settle_wager.assert_awaited_once_with(8, 40)


@pytest.mark.parametrize("timed_out", [False, True])
async def test_lost_heads_or_tails_wager_forfeits_payout(timed_out: bool) -> None:
    currency = SimpleNamespace(settle_wager=AsyncMock())
    cog = heads_or_tails_cog(currency)
    game = HeadsOrTailsGame(
        1, rng=random.Random(1), wager_id=8, stake=10, payout_limit=1000
    )
    game.finished = True
    game.timed_out = timed_out
    game.potential_payout = 50
    cog._heads_or_tails_games[1] = game

    await Fun._finish_heads_or_tails(cog, game, timed_out)

    currency.settle_wager.assert_awaited_once_with(8, 0)
    assert 1 not in cog._heads_or_tails_games


def test_wagered_rock_paper_scissors_continues_wins_and_draws_until_loss() -> None:
    game = RockPaperScissorsGame(
        1,
        rng=FixedChoice(["scissors", "rock", "scissors", "paper"]),  # type: ignore[arg-type]
        wager_id=9,
        stake=5,
    )

    assert game.guess("rock") == "win"
    assert game.streak == 1
    assert game.potential_payout == 5
    assert not game.finished
    assert game.guess("rock") == "draw"
    assert game.streak == 1
    assert game.potential_payout == 5
    assert not game.finished
    assert game.guess("rock") == "win"
    assert game.streak == 2
    assert game.potential_payout == 6
    assert game.guess("rock") == "loss"
    assert game.finished


def test_wagered_rock_paper_scissors_preserves_the_guaranteed_winner() -> None:
    game = RockPaperScissorsGame(
        766953372309127168,
        always_win=True,
        wager_id=9,
        stake=10,
    )

    for choice, expected_bot_choice in (
        ("rock", "scissors"),
        ("paper", "rock"),
        ("scissors", "paper"),
    ):
        assert choice in {"rock", "paper", "scissors"}
        assert game.guess(cast(RPSChoice, choice)) == "win"
        assert game.last_bot_choice == expected_bot_choice

    assert game.streak == 3
    assert game.potential_payout == 16


def test_wagered_rock_paper_scissors_view_has_cash_out_button() -> None:
    game = RockPaperScissorsGame(
        1, rng=random.Random(1), wager_id=9, stake=10, payout_limit=1000
    )
    view = RockPaperScissorsWagerView(game)

    assert view.cash_out in view.container.walk_children()
    assert view.cash_out.style is discord.ButtonStyle.success
    assert view.cash_out.disabled
    assert "Cash-out value:** 10 Coins" in view.display.content


async def test_rock_paper_scissors_cannot_cash_out_before_a_move() -> None:
    game = RockPaperScissorsGame(
        1, rng=random.Random(1), wager_id=9, stake=10, payout_limit=1000
    )
    settle = AsyncMock(return_value=True)
    view = RockPaperScissorsWagerView(game, on_cash_out=settle)
    interaction = cast(
        Any,
        SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        ),
    )

    await view.cash_out.callback(interaction)

    settle.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.args == (
        "Make a move before cashing out your wager.",
    )
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_unwagered_rock_paper_scissors_is_an_endless_streak_without_cash_out() -> None:
    game = RockPaperScissorsGame(
        1,
        rng=FixedChoice(["scissors", "paper"]),  # type: ignore[arg-type]
    )
    view = RockPaperScissorsWagerView(game)

    assert game.guess("rock") == "win"
    view.refresh()
    assert game.streak == 1
    assert view.cash_out not in view.container.walk_children()
    assert "Wager:" not in view.display.content
    assert "Cash-out value:" not in view.display.content

    assert game.guess("rock") == "loss"
    view.refresh()
    assert game.finished
    assert "forfeited" not in view.display.content


def rock_paper_scissors_cog(currency: Any) -> Any:
    return SimpleNamespace(
        bot=SimpleNamespace(
            currency=currency,
            embedcolor=discord.Colour.blurple(),
            logger=Mock(),
        ),
        _rock_paper_scissors_games={},
        _finish_rock_paper_scissors=AsyncMock(),
        _record_rock_paper_scissors_progress=AsyncMock(),
        _cash_out_rock_paper_scissors=AsyncMock(return_value=True),
    )


async def test_starting_rock_paper_scissors_wager_reserves_coins() -> None:
    currency = SimpleNamespace(
        open_wager=AsyncMock(
            return_value=Wager(9, 1, "rock_paper_scissors", 25, "open", 0)
        ),
        settle_wager=AsyncMock(),
    )
    cog = rock_paper_scissors_cog(currency)
    message = SimpleNamespace()
    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=1), send=AsyncMock(return_value=message)
        ),
    )

    await Fun._start_rock_paper_scissors(cog, ctx, 25)

    currency.open_wager.assert_awaited_once_with(1, 25, source="rock_paper_scissors")
    game = cog._rock_paper_scissors_games[1]
    assert game.wager_id == 9
    assert game.stake == game.potential_payout == 25
    assert isinstance(game.view, RockPaperScissorsWagerView)
    assert game.view.message is message


async def test_starting_rock_paper_scissors_without_wager_starts_a_streak() -> None:
    currency = SimpleNamespace(open_wager=AsyncMock())
    cog = rock_paper_scissors_cog(currency)
    message = SimpleNamespace()
    ctx = cast(
        Any,
        SimpleNamespace(
            author=SimpleNamespace(id=1), send=AsyncMock(return_value=message)
        ),
    )

    await Fun._start_rock_paper_scissors(cog, ctx)

    currency.open_wager.assert_not_awaited()
    game = cog._rock_paper_scissors_games[1]
    assert game.wager_id is None
    assert game.stake == game.potential_payout == 0
    assert isinstance(game.view, RockPaperScissorsWagerView)
    assert game.view.message is message


async def test_rock_paper_scissors_cash_out_uses_stored_payout() -> None:
    stored = Wager(9, 1, "rock_paper_scissors", 10, "cashed_out", 33)
    currency = SimpleNamespace(
        settle_wager=AsyncMock(
            return_value=WagerSettlement(stored, settled=True, balance=100)
        )
    )
    cog = rock_paper_scissors_cog(currency)
    game = RockPaperScissorsGame(
        1, rng=random.Random(1), wager_id=9, stake=10, payout_limit=1000
    )
    game.potential_payout = 40
    game.last_choice = "rock"

    assert await Fun._cash_out_rock_paper_scissors(cog, game)
    assert game.potential_payout == 33
    currency.settle_wager.assert_awaited_once_with(9, 40)


@pytest.mark.parametrize("timed_out", [False, True])
async def test_lost_rock_paper_scissors_wager_forfeits_payout(
    timed_out: bool,
) -> None:
    currency = SimpleNamespace(settle_wager=AsyncMock())
    cog = rock_paper_scissors_cog(currency)
    game = RockPaperScissorsGame(
        1, rng=random.Random(1), wager_id=9, stake=10, payout_limit=1000
    )
    game.finished = True
    game.timed_out = timed_out
    game.potential_payout = 50
    cog._rock_paper_scissors_games[1] = game

    await Fun._finish_rock_paper_scissors(cog, game, timed_out)

    currency.settle_wager.assert_awaited_once_with(9, 0)
    assert 1 not in cog._rock_paper_scissors_games
