from __future__ import annotations

import random
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from PIL import Image
from test_support import not_none, require_type

# Test doubles supply only the Discord/service fields exercised by each test.
from extensions.context import Context
from extensions.fun import Fun
from extensions.fun.blackjack import (
    BLACKJACK_MAX_BID,
    BLACKJACK_MIN_BID,
    BlackjackGame,
    BlackjackPrivateView,
    BlackjackView,
    hand_value,
    is_blackjack,
    render_blackjack_board,
)
from extensions.fun.streak_games import PlayingCard, quarter_size_cards


def test_hand_value_counts_aces_as_eleven_when_safe() -> None:
    cards = quarter_size_cards()
    ace = next(card for card in cards if card.rank == "ace")
    nine = next(card for card in cards if card.rank == "9")
    king = next(card for card in cards if card.rank == "king")
    assert hand_value([ace, nine]) == 20
    assert hand_value([ace, nine, king]) == 20
    assert is_blackjack([ace, next(card for card in cards if card.rank == "king")])


def test_blackjack_double_draws_once_and_stands() -> None:
    cards = quarter_size_cards()
    # The custom deck is consumed from the end after shuffle.  This puts the
    # player's opening cards at 10 and 6, dealer cards at 9 and 7, then 5 and
    # 2 for the double draw and the dealer's required draw.
    ten = next(card for card in cards if card.rank == "10")
    six = next(card for card in cards if card.rank == "6")
    nine = next(card for card in cards if card.rank == "9")
    seven = next(card for card in cards if card.rank == "7")
    five = next(card for card in cards if card.rank == "5")
    two = next(card for card in cards if card.rank == "2")
    deck = (two, five, seven, nine, six, ten)

    class ReverseShuffle:
        def shuffle(self, values: list[PlayingCard]) -> None:
            values[:] = deck

    game = BlackjackGame.new(
        1,
        100,
        7,
        rng=cast(Any, ReverseShuffle()),
        deck=deck,
    )
    assert game.can_double
    assert game.double()
    assert game.doubled
    assert game.finished
    assert game.stake_total == 200
    assert len(game.player_hand) == 3
    assert game.result == "win"
    # The opening wager is debited before play; a normal win returns the
    # complete 1.5x amount (stake plus a 50% profit).
    assert game.settlement_payout() == 300


def test_blackjack_uses_one_finite_shoe_without_recycling_cards() -> None:
    cards = quarter_size_cards()[:5]
    game = BlackjackGame.new(1, deck=cards, rng=random.Random(3))
    dealt = [*game.player_hand, *game.dealer_hand]

    assert len({(card.suit, card.rank) for card in dealt}) == len(dealt)
    assert len(game._remaining_cards) == 1
    drawn = game._draw()
    assert (drawn.suit, drawn.rank) not in {(card.suit, card.rank) for card in dealt}
    with pytest.raises(RuntimeError, match="shoe is out of cards"):
        game._draw()


def test_blackjack_discards_duplicate_cards_from_a_custom_shoe() -> None:
    cards = quarter_size_cards()[:4]
    game = BlackjackGame.new(
        1,
        deck=(cards[0], cards[0], cards[1], cards[2], cards[3]),
        rng=random.Random(4),
    )
    dealt = [*game.player_hand, *game.dealer_hand]

    assert len(game.deck) == 4
    assert len({(card.suit, card.rank) for card in dealt}) == 4


def test_blackjack_regular_win_returns_one_and_a_half_times_stake() -> None:
    game = BlackjackGame.new(1, 100)
    game.result = "win"
    game.finished = True

    assert game.settlement_payout() == 150


def test_pvp_uses_player_one_as_house_without_a_bot_hand() -> None:
    game = BlackjackGame.new(
        1,
        100,
        players={1: "house", 2: "player"},
        house_player_id=1,
    )

    assert game.players == (1, 2)
    assert game.house_player_id == 1
    assert game.dealer_hand is game.player_hands[1]
    assert len(game.dealer_hand) == 2
    assert len(game._remaining_cards) == len(game.deck) - 4
    assert game.current_player_id == 2
    assert not game.can_act(1)
    assert game.can_act(2)


def test_pvp_house_is_rendered_as_dealer_and_player_two_acts_first() -> None:
    game = BlackjackGame.new(
        1,
        players={1: "house", 2: "player"},
        house_player_id=1,
    )
    view = BlackjackView(game)

    # Player one is the house-side hand. Render it once as the dealer instead
    # of duplicating the same hand as both ``Dealer`` and ``Player 1``.
    assert "**Dealer (house):**" in view.display.content
    assert "**house:**" not in view.display.content
    assert "**player:**" in view.display.content


def test_pvp_dealer_can_hit_or_stand_after_player_finishes() -> None:
    cards = quarter_size_cards()
    house_ten = next(card for card in cards if card.rank == "10")
    house_two = next(card for card in cards if card.rank == "2")
    player_ten = next(
        card for card in cards if card.rank == "10" and card.suit != house_ten.suit
    )
    player_nine = next(
        card
        for card in cards
        if card.rank == "9" and card.suit not in {house_ten.suit, player_ten.suit}
    )
    five = next(card for card in cards if card.rank == "5")
    game = BlackjackGame.new(
        1,
        players={1: "dealer", 2: "player"},
        house_player_id=1,
    )
    game.player_hands = {
        1: [house_ten, house_two],
        2: [player_ten, player_nine],
    }
    game.player_hand = game.player_hands[1]
    game.dealer_hand = game.player_hands[1]
    game._remaining_cards = [five]
    game.player_stood.clear()
    game.current_player_index = 1

    assert game.stand_for(2)
    assert game.current_player_id == 1
    assert game.can_act(1)
    assert not game.finished

    assert game.hit_for(1)
    assert game.dealer_total == 17
    assert game.current_player_id == 1
    assert not game.finished

    assert game.stand_for(1)
    assert game.finished
    assert game.player_results[2] == "win"


def test_pvp_removes_double_from_public_and_private_controls() -> None:
    game = BlackjackGame.new(
        1,
        players={1: "dealer", 2: "player"},
        house_player_id=1,
    )
    view = BlackjackView(game)
    action_row = next(
        child
        for child in view.container.children
        if isinstance(child, discord.ui.ActionRow)
    )
    assert [
        require_type(child, discord.ui.Button).label for child in action_row.children
    ] == [
        "Hit",
        "Stand",
        "View cards",
    ]
    assert view.double_button is None

    private_view = BlackjackPrivateView(view, 2)
    private_row = next(
        child
        for child in private_view.container.children
        if isinstance(child, discord.ui.ActionRow)
    )
    assert [
        require_type(child, discord.ui.Button).label for child in private_row.children
    ] == ["Hit", "Stand"]
    assert private_view.double_button is None


def _avatar_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


def test_pvp_board_renders_small_circular_avatar_over_each_hand() -> None:
    game = BlackjackGame.new(
        1,
        players={1: "dealer", 2: "player"},
        house_player_id=1,
        player_avatars={
            1: _avatar_bytes((220, 40, 40)),
            2: _avatar_bytes((40, 80, 220)),
        },
        rng=random.Random(5),
    )
    board = render_blackjack_board(game)
    with Image.open(board) as image:
        # Bundled cards are 219px wide and the portrait is anchored to the
        # top-right of the last card in each two-card row.
        avatar_size = int(219 * 0.34)
        x = 18 + 219 + 12 + 219 - avatar_size - 8 + avatar_size // 2
        dealer_y = 18 + 8 + avatar_size // 2
        player_y = dealer_y + 291 + 32
        assert require_type(image.getpixel((x, dealer_y)), tuple)[:3] == (220, 40, 40)
        assert require_type(image.getpixel((x, player_y)), tuple)[:3] == (40, 80, 220)


def test_pvp_winner_receives_both_stakes_without_double_or_multiplier() -> None:
    cards = quarter_size_cards()
    house_ten = next(card for card in cards if card.rank == "10")
    house_seven = next(card for card in cards if card.rank == "7")
    player_ten = next(
        card for card in cards if card.rank == "10" and card.suit != house_ten.suit
    )
    player_eight = next(card for card in cards if card.rank == "8")
    game = BlackjackGame.new(
        1,
        100,
        players={1: "house", 2: "player"},
        house_player_id=1,
    )
    game.player_hands = {
        1: [house_ten, house_seven],
        2: [player_ten, player_eight],
    }
    game.player_hand = game.player_hands[1]
    game.dealer_hand = game.player_hands[1]
    game.stake_by_player = {1: 100, 2: 100}
    game.player_stood = {1, 2}
    game._remaining_cards = []

    assert not game.can_double_for(2)
    assert not game.double_for(2)
    game._resolve_multiplayer()

    assert game.player_payouts == {1: 0, 2: 200}
    assert game.player_results[2] == "win"


def test_blackjack_push_returns_the_total_stake() -> None:
    cards = quarter_size_cards()
    game = BlackjackGame.new(1, 100, 1, rng=random.Random(4), deck=cards)
    game.result = "push"
    game.finished = True
    assert game.settlement_payout() == 100


def test_blackjack_view_uses_card_gallery_and_three_controls() -> None:
    game = BlackjackGame.new(1, 100, 1, rng=random.Random(2))
    cog = SimpleNamespace(bot=SimpleNamespace(embedcolor=discord.Colour.blurple()))
    view = BlackjackView(game, accent_color=cog.bot.embedcolor)
    assert isinstance(view, discord.ui.LayoutView)
    assert isinstance(view.gallery, discord.ui.MediaGallery)
    assert len(view.gallery.items) == 1  # one composite dealer/player image
    assert len(view.card_files()) == 1
    assert {
        view.hit_button.label,
        view.stand_button.label,
        not_none(view.double_button).label,
    } == {
        "Hit",
        "Stand",
        "Double",
    }
    assert BLACKJACK_MIN_BID == 10
    assert BLACKJACK_MAX_BID == 10_000


def test_solo_blackjack_reveals_player_cards_without_private_control() -> None:
    game = BlackjackGame.new(1, 100, rng=random.Random(7))
    view = BlackjackView(game)

    # Solo/house Blackjack keeps the original public presentation: the
    # player's total is shown in the board text and the composite image, while
    # the dealer's hand remains hidden until the round resolves.
    player_labels = [card.label for card in game.player_hand]
    assert f"**You:** {game.player_total}" in view.display.content
    assert (
        f"**Dealer:** {hand_value([game.dealer_hand[0]])} + Hidden card"
        in view.display.content
    )
    assert "Free game" not in view.display.content
    assert all(label not in view.display.content for label in player_labels)
    assert view.display.content.count("Hidden card") == 1
    action_row = next(
        child
        for child in view.container.children
        if isinstance(child, discord.ui.ActionRow)
    )
    assert len(action_row.children) == 3
    assert view.view_cards_button.parent is None


async def test_blackjack_view_sends_private_cards_ephemerally() -> None:
    """A player's hand must never be sent in the public game message."""

    game = BlackjackGame.new(
        1,
        players={1: "one", 2: "two"},
    )
    view = BlackjackView(game)
    response = SimpleNamespace(
        is_done=lambda: False,
        send_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=response,
    )

    await view.send_private_cards(
        cast("discord.Interaction[discord.Client]", interaction)
    )

    response.send_message.assert_awaited_once()
    kwargs = response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert (
        kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    )
    assert kwargs["files"]
    assert "Your total:" in kwargs["content"]
    assert str(game.total_for(2)) in kwargs["content"]


@pytest.mark.asyncio
async def test_blackjack_view_cards_uses_private_components_v2_panel() -> None:
    game = BlackjackGame.new(1, players={1: "one", 2: "two"})
    view = BlackjackView(game)
    response = SimpleNamespace(
        is_done=lambda: False,
        send_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(return_value=SimpleNamespace()),
    )

    await view.send_private_cards(
        cast("discord.Interaction[discord.Client]", interaction)
    )

    response.send_message.assert_awaited_once()
    kwargs = response.send_message.await_args.kwargs
    assert "content" not in kwargs
    assert isinstance(kwargs["view"], BlackjackPrivateView)
    private_view = kwargs["view"]
    assert private_view.user_id == 2
    assert len(private_view.gallery.items) == 1
    assert {
        private_view.hit_button.label,
        private_view.stand_button.label,
        private_view.double_button.label,
    } == {"Hit", "Stand", "Double"}
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].to_dict() == (
        discord.AllowedMentions.none().to_dict()
    )


def test_blackjack_view_shows_upcards_and_masks_private_cards() -> None:
    game = BlackjackGame.new(1, players={1: "one", 2: "two"})
    view = BlackjackView(game)

    public_text = view.display.content
    assert public_text.count("Hidden card") >= 3
    for user_id in game.participant_ids:
        hand = game.hand_for(user_id)
        assert all(card.label not in public_text for card in hand)
    assert "Hidden cards" in public_text


def test_finished_blackjack_view_uses_dealer_total_and_component_separator() -> None:
    game = BlackjackGame.new(1, 100, players={1: "one", 2: "two"})
    game.player_results = {1: "win", 2: "loss"}
    game.player_payouts = {1: 150, 2: 0}
    game.finished = True
    view = BlackjackView(game)

    text = view.display.content
    assert f"**Dealer:** {game.dealer_total}" in text
    assert all(card.label not in text for card in game.dealer_hand)
    assert "────────────────────" not in text
    assert isinstance(view.separator, discord.ui.Separator)
    assert view.footer.content.startswith("-#")


@pytest.mark.asyncio
async def test_blackjack_rejects_fishie_as_a_pvp_opponent() -> None:
    class StubUser(discord.User):
        def __init__(self, user_id: int, *, bot: bool, name: str) -> None:
            self.id = user_id
            self.bot = bot
            self.name = name

    fishie = StubUser(999, bot=True, name="fishie")
    author = StubUser(111, bot=False, name="author")
    ctx = SimpleNamespace(
        author=author,
        bot=SimpleNamespace(user=fishie),
        send=AsyncMock(),
    )
    command = SimpleNamespace()
    command._start_blackjack = AsyncMock()

    await Fun._blackjack_entry(cast("Fun", command), cast("Context", ctx), fishie)

    command._start_blackjack.assert_not_awaited()
    ctx.send.assert_awaited_once()
    message = ctx.send.await_args.args[0]
    assert "two human players" in message
