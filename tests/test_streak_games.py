from __future__ import annotations

import random

import discord

from extensions.fun.streak_games import (
    HeadsOrTailsGame,
    HeadsOrTailsView,
    HigherOrLowerGame,
    HigherOrLowerView,
    PlayingCard,
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
