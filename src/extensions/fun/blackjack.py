"""Blackjack against Fishie's house.

The game deliberately keeps its mutable board in memory while using the
shared currency wager service for every wallet mutation.  Card images come
from the bundled 25% playing-card assets used by the other card games.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, Literal, Mapping, cast

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from core.currency import (
    EVERYTHING_AMOUNT,
    CoinAmountError,
    InsufficientFunds,
    InvalidAmount,
    parse_coin_amount,
)
from extensions.context import Context
from utils.paths import FILES_ROOT

from .streak_games import PlayingCard, quarter_size_cards

if TYPE_CHECKING:
    from core import Fishie


GAME_TIMEOUT = 10 * 60
BLACKJACK_MIN_BID = 10
# Compatibility export for extensions/tests that still import the historical
# limit.  Wager validation no longer enforces this value; the wallet balance
# and shared payout overflow guard are the effective limits.
BLACKJACK_MAX_BID = 10_000
BLACKJACK_SOURCE = "blackjack"
# Keep an unusual run with a custom/test deck from producing an oversized
# composite image or an invalid interaction payload.
MAX_DISPLAY_CARDS = 10
BLACKJACK_CARD_GAP = 12
BLACKJACK_ROW_GAP = 32
BLACKJACK_PADDING = 18
BLACKJACK_BACK_CARD_PATH = (
    FILES_ROOT / "images" / "cards" / "decks" / "25" / "Red_Deck.png"
)
BLACKJACK_AVATAR_RATIO = 0.34
BLACKJACK_AVATAR_MIN_SIZE = 40
BLACKJACK_AVATAR_MAX_SIZE = 88
BLACKJACK_AVATAR_INSET = 8
BLACKJACK_AVATAR_SHADOW_OFFSET = (4, 5)
BLACKJACK_AVATAR_SHADOW_OPACITY = 180
BLACKJACK_AVATAR_SHADOW_BLUR = 4

BlackjackResult = Literal["blackjack", "win", "push", "loss"]
FinishCallback = Callable[["BlackjackGame"], Awaitable[None]]
DoubleCallback = Callable[["BlackjackGame"], Awaitable[bool]]
DoublePlayerCallback = Callable[["BlackjackGame", int], Awaitable[bool]]


def hand_value(hand: list[PlayingCard]) -> int:
    """Return the best blackjack value for a hand, counting aces flexibly."""

    value = sum(1 if card.value == 14 else min(card.value, 10) for card in hand)
    aces = sum(card.value == 14 for card in hand)
    while aces and value + 10 <= 21:
        value += 10
        aces -= 1
    return value


def is_blackjack(hand: list[PlayingCard]) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


def _card_back(size: tuple[int, int]) -> Image.Image:
    """Load the bundled red deck image for the dealer's hidden card."""

    width, height = size
    try:
        with Image.open(BLACKJACK_BACK_CARD_PATH) as opened:
            return opened.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        # Keep a safe fallback if the optional deck asset is unavailable in a
        # development checkout.
        image = Image.new("RGBA", size, (38, 66, 116, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (2, 2, width - 3, height - 3),
        radius=max(4, min(width, height) // 18),
        outline=(235, 241, 255, 255),
        width=max(2, width // 70),
    )
    inset = max(8, min(width, height) // 14)
    draw.rounded_rectangle(
        (inset, inset, width - inset - 1, height - inset - 1),
        radius=max(3, min(width, height) // 28),
        outline=(170, 204, 245, 255),
        width=max(1, width // 100),
    )
    return image


def _draw_player_avatar(
    canvas: Image.Image,
    data: bytes,
    *,
    x: int,
    y: int,
    size: int,
) -> None:
    """Draw a small circular player portrait over a hand's top-right card."""

    try:
        with Image.open(BytesIO(data)) as opened:
            avatar = ImageOps.fit(
                opened.convert("RGBA"),
                (size, size),
                method=Image.Resampling.LANCZOS,
            )
    except (OSError, ValueError):
        # An avatar should never prevent a Blackjack board from rendering if
        # Discord returns an unsupported or incomplete image.
        return

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    avatar.putalpha(mask)

    # Blur on a same-sized mask clips the shadow at the avatar's edge.  Keep
    # a padded mask/layer so the blur can extend naturally around the whole
    # portrait (particularly at the top-right corner of a card).
    shadow_padding = max(
        BLACKJACK_AVATAR_SHADOW_BLUR * 2,
        abs(BLACKJACK_AVATAR_SHADOW_OFFSET[0]),
        abs(BLACKJACK_AVATAR_SHADOW_OFFSET[1]),
    )
    shadow_size = size + shadow_padding * 2
    shadow_mask = Image.new("L", (shadow_size, shadow_size), 0)
    ImageDraw.Draw(shadow_mask).ellipse(
        (
            shadow_padding,
            shadow_padding,
            shadow_padding + size - 1,
            shadow_padding + size - 1,
        ),
        fill=255,
    )
    shadow_mask = shadow_mask.filter(
        ImageFilter.GaussianBlur(BLACKJACK_AVATAR_SHADOW_BLUR)
    )
    shadow_mask = shadow_mask.point(
        lambda alpha: int(alpha * BLACKJACK_AVATAR_SHADOW_OPACITY / 255)
    )
    shadow = Image.new("RGBA", (shadow_size, shadow_size), (0, 0, 0, 0))
    shadow.putalpha(shadow_mask)
    shadow_x = x + BLACKJACK_AVATAR_SHADOW_OFFSET[0]
    shadow_y = y + BLACKJACK_AVATAR_SHADOW_OFFSET[1]
    canvas.alpha_composite(
        shadow,
        (shadow_x - shadow_padding, shadow_y - shadow_padding),
    )
    canvas.alpha_composite(avatar, (x, y))


def render_blackjack_board(
    game: "BlackjackGame",
    *,
    viewer_id: int | None = None,
    reveal_player_cards: bool = False,
) -> BytesIO:
    """Render a transparent board image.

    The public board shows each player's up-card and uses the card back for
    every other card while a player-vs-player round is active.  A participant
    can see their own complete hand through the private ``View cards`` panel,
    but standing must never reveal that hand to the opponent.  The original
    single-player API remains unchanged when neither option is supplied.
    """

    dealer_cards: list[PlayingCard | None]
    if game.dealer_revealed or (
        game.house_player_id is not None
        and viewer_id is not None
        and int(viewer_id) == int(game.house_player_id)
    ):
        dealer_cards = list(game.dealer_hand)
    else:
        # Preserve the number of cards drawn by a player-as-house hand while
        # masking every card after the public up-card.  Previously only the
        # opening two cards were represented, so additional house hits
        # silently disappeared from the public board instead of showing their
        # backs.
        dealer_cards = (
            [game.dealer_hand[0], *([None] * (len(game.dealer_hand) - 1))]
            if game.dealer_hand
            else [None]
        )
    # ``player_hands`` is populated for a player-vs-player round.  Fall back to
    # the historical ``player_hand`` field for house games and tests.
    hands = game.player_hands or {game.user_id: game.player_hand}
    if game.house_player_id is not None:
        # The house participant is represented by the top (dealer) row.  Do
        # not duplicate their cards in the player rows below it.
        hands = {
            user_id: hand
            for user_id, hand in hands.items()
            if int(user_id) != int(game.house_player_id)
        }
    dealer_avatar = (
        game.player_avatars.get(int(game.house_player_id))
        if game.house_player_id is not None
        else None
    )
    player_rows: list[tuple[int, list[PlayingCard | None]]] = []
    for user_id, hand in hands.items():
        visible = (
            # A house game is public: the player's complete hand was shown in
            # the original solo Blackjack UI.  Player-vs-player rounds keep
            # every card after the public up-card private for the whole live
            # round, including after a player stands.  ``viewer_id`` is used
            # by the private panel to reveal only that participant's hand.
            not game.multiplayer
            or game.finished
            or reveal_player_cards
            or viewer_id is not None
            and int(user_id) == int(viewer_id)
        )
        if visible:
            player_rows.append((int(user_id), list(hand)))
        elif len(hand) >= 2:
            # Keep the opening/up-card visible and mask the second card and
            # every card drawn afterwards.  The placeholders are retained so
            # the public board still communicates how many cards are held.
            player_rows.append((int(user_id), [hand[0], *([None] * (len(hand) - 1))]))
        else:
            player_rows.append((int(user_id), [hand[0]] if hand else [None]))
    if not player_rows:
        player_rows = [(int(game.user_id), [None])]

    # A hand can grow unusually large with a custom/test deck. Keep the image
    # within the same display cap used by the old attachment gallery.
    # Cap the total number of cards represented in the image.  This protects
    # Discord's attachment size limits when custom/test shoes are used while
    # retaining at least one card in each row.
    player_card_count = sum(len(row) for _, row in player_rows)
    if len(dealer_cards) + player_card_count > MAX_DISPLAY_CARDS:
        dealer_limit = max(1, min(len(dealer_cards), MAX_DISPLAY_CARDS - 1))
        dealer_cards = dealer_cards[:dealer_limit]
        remaining = max(1, MAX_DISPLAY_CARDS - len(dealer_cards))
        capped_rows: list[tuple[int, list[PlayingCard | None]]] = []
        for user_id, row in player_rows:
            if remaining <= 0:
                break
            take = max(1, min(len(row), remaining))
            capped_rows.append((user_id, row[:take]))
            remaining -= take
        player_rows = capped_rows or [(int(game.user_id), [None])]
    if not dealer_cards:
        dealer_cards = [None]
    if not player_rows:
        player_rows = [(int(game.user_id), [None])]

    source_card = next(
        (
            card
            for card in (
                *dealer_cards,
                *(card for _, row in player_rows for card in row),
            )
            if card is not None
        ),
        None,
    )
    if source_card is None:  # pragma: no cover - a dealt hand always has cards.
        raise RuntimeError("Blackjack board has no card dimensions")
    with Image.open(source_card.path) as opened:
        sample = opened.convert("RGBA")
    card_width, card_height = sample.size

    def row_width(cards: list[PlayingCard | None]) -> int:
        return len(cards) * card_width + (len(cards) - 1) * BLACKJACK_CARD_GAP

    width = (
        max(
            [
                row_width(dealer_cards),
                *(row_width(row) for _, row in player_rows),
            ]
        )
        + 2 * BLACKJACK_PADDING
    )
    row_count = 1 + len(player_rows)
    height = (
        2 * BLACKJACK_PADDING
        + row_count * card_height
        + (row_count - 1) * BLACKJACK_ROW_GAP
    )
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    def draw_row(
        cards: list[PlayingCard | None],
        top: int,
        avatar_data: bytes | None = None,
    ) -> None:
        x = (width - row_width(cards)) // 2
        last_card_x = x + (len(cards) - 1) * (card_width + BLACKJACK_CARD_GAP)
        for card in cards:
            if card is None:
                rendered = _card_back((card_width, card_height))
            else:
                with Image.open(card.path) as opened:
                    rendered = opened.convert("RGBA")
            canvas.alpha_composite(rendered, (x, top))
            x += card_width + BLACKJACK_CARD_GAP
        if avatar_data is not None and game.house_player_id is not None:
            avatar_size = max(
                BLACKJACK_AVATAR_MIN_SIZE,
                min(
                    BLACKJACK_AVATAR_MAX_SIZE,
                    int(min(card_width, card_height) * BLACKJACK_AVATAR_RATIO),
                ),
            )
            _draw_player_avatar(
                canvas,
                avatar_data,
                x=last_card_x + card_width - avatar_size - BLACKJACK_AVATAR_INSET,
                y=top + BLACKJACK_AVATAR_INSET,
                size=avatar_size,
            )

    dealer_cards_top = BLACKJACK_PADDING
    draw_row(dealer_cards, dealer_cards_top, dealer_avatar)
    row_top = dealer_cards_top + card_height + BLACKJACK_ROW_GAP
    for user_id, row in player_rows:
        draw_row(row, row_top, game.player_avatars.get(user_id))
        row_top += card_height + BLACKJACK_ROW_GAP

    output = BytesIO()
    canvas.save(output, format="PNG")
    output.seek(0)
    return output


@dataclass(slots=True)
class BlackjackGame:
    """One in-memory Blackjack round.

    The original implementation represented a house game with
    ``player_hand``.  ``player_hands`` and the turn/result maps extend that
    model to two or more human players while keeping the old attributes and
    methods available to callers and tests.
    """

    user_id: int
    stake: int = 0
    wager_id: int = 0
    deck: tuple[PlayingCard, ...] = field(default_factory=quarter_size_cards)
    rng: random.Random | random.SystemRandom = field(
        default_factory=random.SystemRandom, repr=False
    )
    player_hand: list[PlayingCard] = field(default_factory=list)
    dealer_hand: list[PlayingCard] = field(default_factory=list)
    wager_ids: list[int] = field(default_factory=list)
    stake_total: int = 0
    doubled: bool = False
    finished: bool = False
    timed_out: bool = False
    result: BlackjackResult | None = None
    settlement_error: bool = False
    settled: bool = False
    payout: int = 0
    view: BlackjackView | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Multiplayer state.  A normal house game leaves these empty and uses the
    # compatibility fields above.  The controller can pass ``players`` to
    # :meth:`new` to deal two cards to each player from the same shoe.
    players: tuple[int, ...] = field(default_factory=tuple)
    # In a player-vs-player round the first participant can act as the house.
    # When set, that participant's hand is also exposed through
    # ``dealer_hand`` for the existing rendering/settlement helpers; no
    # Fishie/bot hand is dealt or inserted into ``players``.
    house_player_id: int | None = None
    player_names: dict[int, str] = field(default_factory=dict)
    player_avatars: dict[int, bytes] = field(default_factory=dict, repr=False)
    player_hands: dict[int, list[PlayingCard]] = field(default_factory=dict)
    player_stood: set[int] = field(default_factory=set)
    player_results: dict[int, BlackjackResult] = field(default_factory=dict)
    player_payouts: dict[int, int] = field(default_factory=dict)
    player_actions: dict[int, str] = field(default_factory=dict)
    stake_by_player: dict[int, int] = field(default_factory=dict)
    doubled_players: set[int] = field(default_factory=set)
    current_player_index: int = 0
    last_action: str | None = None
    last_actor_id: int | None = None

    def __post_init__(self) -> None:
        # A zero stake is a valid free game.  Positive stakes still honour the
        # normal minimum; negative amounts remain invalid.
        if self.stake < 0 or (0 < self.stake < BLACKJACK_MIN_BID):
            raise ValueError("Blackjack stake is below the minimum.")
        if not self.wager_ids and self.wager_id:
            self.wager_ids.append(int(self.wager_id))
        self.stake_total = self.stake
        if self.players:
            self.players = tuple(dict.fromkeys(int(player) for player in self.players))
        if self.house_player_id is not None and not self.players:
            raise ValueError("A house player requires a multiplayer round.")
        unique_cards: list[PlayingCard] = []
        seen_cards: set[tuple[str, str]] = set()
        for card in self.deck:
            card_key = (card.suit.casefold(), card.rank.casefold())
            if card_key in seen_cards:
                continue
            seen_cards.add(card_key)
            unique_cards.append(card)
        self.deck = tuple(unique_cards)
        required_cards = (
            len(self.players) * 2 + (0 if self.house_player_id is not None else 2)
            if self.players
            else 4
        )
        if len(self.deck) < required_cards:
            raise ValueError("The Blackjack deck does not contain enough cards.")
        self._deal_initial()

    @classmethod
    def new(
        cls,
        user_id: int,
        stake: int = 0,
        wager_id: int = 0,
        *,
        rng: random.Random | random.SystemRandom | None = None,
        deck: tuple[PlayingCard, ...] | None = None,
        players: Mapping[int, str] | Iterable[int] | None = None,
        house_player_id: int | None = None,
        player_avatars: Mapping[int, bytes] | None = None,
    ) -> BlackjackGame:
        player_ids: tuple[int, ...] = ()
        player_names: dict[int, str] = {}
        if players is not None:
            if isinstance(players, Mapping):
                player_names = {int(key): str(value) for key, value in players.items()}
                player_ids = tuple(player_names)
            else:
                player_ids = tuple(int(player) for player in players)
                player_names = {player: str(player) for player in player_ids}
        return cls(
            user_id=user_id,
            stake=stake,
            wager_id=wager_id,
            deck=deck or quarter_size_cards(),
            rng=rng or random.SystemRandom(),
            players=player_ids,
            house_player_id=(
                int(house_player_id) if house_player_id is not None else None
            ),
            player_names=player_names,
            player_avatars={
                int(user_id): data
                for user_id, data in (player_avatars or {}).items()
                if isinstance(data, bytes)
            },
        )

    def _deal_initial(self) -> None:
        cards = list(self.deck)
        self.rng.shuffle(cards)
        if self.players:
            self.player_hands = {
                player_id: [cards.pop(), cards.pop()] for player_id in self.players
            }
            # Keep player_hand pointing to the first hand for old helpers and
            # rendering code that assumes a single player.
            self.player_hand = self.player_hands[self.players[0]]
            if self.house_player_id is not None:
                if self.house_player_id not in self.player_hands:
                    raise ValueError("The house player must be a participant.")
                # PvP rounds use the first human's hand as the house hand.  An
                # alias keeps the established ``dealer_total`` and board
                # helpers working without dealing an invisible bot hand.
                self.dealer_hand = self.player_hands[self.house_player_id]
            else:
                self.dealer_hand = [cards.pop(), cards.pop()]
        else:
            self.player_hand = [cards.pop(), cards.pop()]
            self.dealer_hand = [cards.pop(), cards.pop()]
        self._remaining_cards = cards
        self.current_player_index = 0
        self.player_stood.clear()
        if self.house_player_id is not None:
            # Player 1 is the house in PvP mode.  The house follows the
            # other human's turn and then gets the same Hit/Stand choices
            # instead of drawing automatically.
            if self.players:
                house_index = self.players.index(self.house_player_id)
                self.current_player_index = (house_index + 1) % len(self.players)
        self.player_results.clear()
        self.player_payouts.clear()
        self.player_actions.clear()
        self.stake_by_player = {player_id: self.stake for player_id in self.players}
        self.doubled_players.clear()
        self.last_action = None
        self.last_actor_id = None
        # A natural 21 is not resolved automatically. The player still has
        # to press Stand so the hidden dealer card is not revealed for free;
        # this also lets a natural 21 correctly push against a dealer 21.

    # This is intentionally private and populated when a game is dealt.  A
    # fresh shoe per game avoids impossible duplicate cards while keeping the
    # public dataclass API small.
    _remaining_cards: list[PlayingCard] = field(default_factory=list, repr=False)

    @property
    def multiplayer(self) -> bool:
        """Whether this round has more than one human/player hand."""

        return bool(self.player_hands and len(self.player_hands) > 1)

    @property
    def participant_ids(self) -> tuple[int, ...]:
        """Players in turn order (or the legacy house player)."""

        return self.players or (self.user_id,)

    @property
    def current_player_id(self) -> int | None:
        """ID of the player whose action is currently required."""

        participants = self.participant_ids
        if not participants or self.finished:
            return None
        # A game may advance over stood/busted players; this property is kept
        # defensive so a stale index never raises during an interaction.
        return participants[self.current_player_index % len(participants)]

    def hand_for(self, user_id: int) -> list[PlayingCard]:
        """Return a participant's hand, including the legacy house hand."""

        if self.player_hands:
            try:
                return self.player_hands[int(user_id)]
            except KeyError as exc:
                raise ValueError("That user is not playing this round.") from exc
        if int(user_id) != int(self.user_id):
            raise ValueError("That user is not playing this round.")
        return self.player_hand

    def total_for(self, user_id: int) -> int:
        return hand_value(self.hand_for(user_id))

    def can_act(self, user_id: int) -> bool:
        """Return whether ``user_id`` may press a gameplay control now."""

        if self.finished or int(user_id) not in self.participant_ids:
            return False
        if self.current_player_id != int(user_id):
            return False
        hand = self.hand_for(int(user_id))
        return int(user_id) not in self.player_stood and hand_value(hand) <= 21

    def private_card_text(self, user_id: int) -> str:
        """Build the private hand summary without exposing card names."""

        hand = self.hand_for(int(user_id))
        return f"**Your total:** {hand_value(hand)}"

    def _advance_player_turn(self) -> None:
        """Move to the next live player, resolving once everyone is done."""

        if not self.players or self.finished:
            return
        for _ in range(len(self.players)):
            if all(
                player_id in self.player_stood or player_id in self.player_results
                for player_id in self.players
            ):
                self._resolve_multiplayer()
                return
            self.current_player_index = (self.current_player_index + 1) % len(
                self.players
            )
            current = self.current_player_id
            if current is not None and current not in self.player_stood:
                return

    def _resolve_multiplayer(self) -> None:
        """Resolve the dealer and all participant results after final actions."""

        if not self.players or self.finished:
            return
        # A PvP round may designate player 1 as the house. That hand is
        # resolved exactly where the dealer chose to stand or busted; there
        # is no invisible Fishie/bot hand in that mode. Legacy multiplayer
        # games (created without ``house_player_id``) retain the automatic
        # dealer draw behavior.
        if self.house_player_id is None:
            while self.dealer_total < 17:
                self.dealer_hand.append(self._draw())
        dealer = self.dealer_total
        dealer_blackjack = is_blackjack(self.dealer_hand)
        participant_results: dict[int, BlackjackResult] = {}
        for player_id in self.players:
            if self.house_player_id is not None and player_id == self.house_player_id:
                continue
            hand = self.player_hands[player_id]
            player = hand_value(hand)
            if player > 21:
                result: BlackjackResult = "loss"
            elif dealer_blackjack and is_blackjack(hand):
                result = "push"
            elif is_blackjack(hand) and not dealer_blackjack:
                result = "blackjack"
            elif dealer_blackjack:
                result = "loss"
            elif dealer > 21 or player > dealer:
                result = "win"
            elif player == dealer:
                result = "push"
            else:
                result = "loss"
            participant_results[player_id] = result
            stake = self.stake_by_player.get(player_id, self.stake)
            if result == "push":
                payout = stake
            elif result == "blackjack":
                payout = stake + stake * 3 // 2
            elif result == "win":
                # The wager has already been debited from the wallet.  A
                # regular win therefore returns the stake plus a 50% profit
                # (1.5x total), rather than crediting a full 2x return.
                payout = stake * 3 // 2
            else:
                payout = 0
            self.player_payouts[player_id] = payout
        if self.house_player_id is not None:
            house_id = int(self.house_player_id)
            # A two-player round is a zero-sum outcome from the house's point
            # of view: the house wins when its opponent loses, pushes on a
            # tie, and loses when the opponent wins.
            opponent_results = list(participant_results.values())
            if any(result in {"win", "blackjack"} for result in opponent_results):
                house_result: BlackjackResult = "loss"
            elif opponent_results and all(
                result == "push" for result in opponent_results
            ):
                house_result = "push"
            else:
                house_result = "win"
            participant_results[house_id] = house_result
            # Player-versus-player rounds are winner-takes-all. Both wagers
            # were already debited, so the winner receives the complete pool;
            # Blackjack's house multipliers and Double action do not apply.
            pool = sum(
                max(0, int(self.stake_by_player.get(player_id, self.stake)))
                for player_id in self.players
            )
            winner_id = next(
                (
                    player_id
                    for player_id, outcome in participant_results.items()
                    if outcome in {"win", "blackjack"}
                ),
                None,
            )
            for player_id in self.players:
                if winner_id is not None:
                    self.player_payouts[player_id] = (
                        pool if player_id == winner_id else 0
                    )
                else:
                    self.player_payouts[player_id] = max(
                        0, int(self.stake_by_player.get(player_id, self.stake))
                    )
        self.player_results.update(participant_results)
        self.result = self.player_results.get(self.user_id, "loss")
        self.payout = self.player_payouts.get(self.user_id, 0)
        self.finished = True

    def hit_for(self, user_id: int) -> bool:
        """Apply a hit for one participant and advance the turn if needed."""

        if not self.can_act(user_id):
            return False
        hand = self.hand_for(user_id)
        hand.append(self._draw())
        self.player_actions[int(user_id)] = "hit"
        self.last_action = "hit"
        self.last_actor_id = int(user_id)
        if hand_value(hand) > 21:
            self.player_results[int(user_id)] = "loss"
            self.player_stood.add(int(user_id))
            if self.players:
                self._advance_player_turn()
        return hand_value(hand) <= 21

    def stand_for(self, user_id: int) -> bool:
        """Stand for one participant and advance the turn."""

        if not self.can_act(user_id):
            return False
        self.player_stood.add(int(user_id))
        self.player_actions[int(user_id)] = "stand"
        self.last_action = "stand"
        self.last_actor_id = int(user_id)
        if self.players:
            self._advance_player_turn()
        return True

    def double_for(self, user_id: int) -> bool:
        """Draw one final card after doubling a participant's stake."""

        if self.house_player_id is not None:
            return False
        if not self.can_act(user_id) or len(self.hand_for(user_id)) != 2:
            return False
        self.doubled_players.add(int(user_id))
        self.stake_by_player[int(user_id)] = self.stake_by_player.get(
            int(user_id), self.stake
        ) + self.stake_by_player.get(int(user_id), self.stake)
        self.player_actions[int(user_id)] = "double"
        self.last_action = "double"
        self.last_actor_id = int(user_id)
        hand = self.hand_for(user_id)
        hand.append(self._draw())
        if hand_value(hand) > 21:
            self.player_results[int(user_id)] = "loss"
        self.player_stood.add(int(user_id))
        if self.players:
            self._advance_player_turn()
        return True

    @property
    def player_total(self) -> int:
        return hand_value(self.player_hand)

    @property
    def dealer_total(self) -> int:
        return hand_value(self.dealer_hand)

    @property
    def can_double(self) -> bool:
        return not self.finished and not self.doubled and len(self.player_hand) == 2

    def can_double_for(self, user_id: int) -> bool:
        """Whether a participant may double on their opening two cards."""

        if self.house_player_id is not None:
            return False
        if self.player_hands:
            return (
                self.can_act(user_id)
                and int(user_id) not in self.doubled_players
                and len(self.hand_for(user_id)) == 2
            )
        return self.can_double and int(user_id) == int(self.user_id)

    @property
    def dealer_revealed(self) -> bool:
        return self.finished

    def _draw(self) -> PlayingCard:
        if not self._remaining_cards:
            raise RuntimeError("The Blackjack shoe is out of cards.")
        return self._remaining_cards.pop()

    def hit(self) -> bool:
        """Draw for the player; return whether the hand remains live."""

        if self.finished:
            return False
        self.player_hand.append(self._draw())
        if self.player_total > 21:
            self.result = "loss"
            self.finished = True
            return False
        return True

    def double(self) -> bool:
        """Double the reserved stake and draw exactly one final card."""

        if not self.can_double:
            return False
        self.doubled = True
        self.stake_total += self.stake
        self.hit()
        if not self.finished:
            self.stand()
        return True

    def stand(self) -> BlackjackResult:
        """Resolve the dealer and return the result for the player."""

        if self.finished and self.result is not None:
            return self.result
        while self.dealer_total < 17:
            self.dealer_hand.append(self._draw())
        player = self.player_total
        dealer = self.dealer_total
        player_blackjack = is_blackjack(self.player_hand)
        dealer_blackjack = is_blackjack(self.dealer_hand)
        if player_blackjack and dealer_blackjack:
            result: BlackjackResult = "push"
        elif player_blackjack:
            result = "blackjack"
        elif dealer_blackjack:
            result = "loss"
        elif player > 21:
            result = "loss"
        elif dealer > 21 or player > dealer:
            result = "win"
        elif player == dealer:
            result = "push"
        else:
            result = "loss"
        self.result = result
        self.finished = True
        return result

    def timeout(self) -> None:
        if self.finished:
            return
        self.timed_out = True
        if self.players:
            # A timeout forfeits every unfinished player hand.  Reveal the
            # dealer for the final board, but do not award any payout.  A bot
            # dealer still follows the normal draw rule; a player-as-house
            # hand is left exactly where the dealer stopped acting.
            if self.house_player_id is None:
                while self.dealer_total < 17:
                    self.dealer_hand.append(self._draw())
            for player_id in self.players:
                self.player_results[player_id] = "loss"
                self.player_payouts[player_id] = 0
                self.player_stood.add(player_id)
            self.result = self.player_results.get(self.user_id, "loss")
            self.payout = 0
        else:
            self.result = "loss"
        self.finished = True

    def settlement_payout(self) -> int:
        """Return the amount to credit (the wager was already debited)."""

        if self.result == "push":
            return self.stake_total
        if self.result == "blackjack":
            # Standard 3:2 blackjack payout, rounded down to whole Coins.
            return self.stake_total + (self.stake_total * 3 // 2)
        if self.result == "win":
            # Wagers are debited up front, so this is the complete 1.5x
            # return (stake plus a 50% profit).
            return self.stake_total * 3 // 2
        return 0


class BlackjackPrivateView(discord.ui.LayoutView):
    """Private Components V2 panel for one player's Blackjack hand.

    The public game message intentionally masks opening cards while the round
    is live.  This view is sent ephemerally to the participant who requests
    their cards and keeps the same Hit/Stand controls available there.
    It is retained by :class:`BlackjackView` so subsequent actions edit the
    existing ephemeral message instead of sending a new one for every click.
    """

    filename = "blackjack-private.png"

    def __init__(
        self,
        parent: "BlackjackView",
        user_id: int,
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.parent = parent
        self.game = parent.game
        self.user_id = int(user_id)
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(self._text())
        self.gallery = discord.ui.MediaGallery()
        self.hit_button = discord.ui.Button(
            label="Hit", style=discord.ButtonStyle.secondary
        )
        self.stand_button = discord.ui.Button(
            label="Stand", style=discord.ButtonStyle.primary
        )
        self.double_button: discord.ui.Button | None = None
        self.hit_button.callback = self._hit
        self.stand_button.callback = self._stand
        controls: list[discord.ui.Button] = [self.hit_button, self.stand_button]
        if self.game.house_player_id is None:
            self.double_button = discord.ui.Button(
                label="Double", style=discord.ButtonStyle.success
            )
            self.double_button.callback = self._double
            controls.append(self.double_button)
        self.container = discord.ui.Container(
            self.display,
            self.gallery,
            discord.ui.ActionRow(*controls),
            accent_color=parent.container.accent_color,
        )
        self.add_item(self.container)
        self.refresh()

    def _text(self) -> str:
        if self.game.finished:
            return f"## Blackjack\n{self.game.private_card_text(self.user_id)}"
        current = self.game.current_player_id
        if current == self.user_id:
            state = "It is your turn."
        elif current is None:
            state = "Waiting for the round to finish."
        else:
            name = self.game.player_names.get(current, str(current))
            state = f"Waiting for **{discord.utils.escape_markdown(name)}** to play."
        return f"## Blackjack · Your cards\n{self.game.private_card_text(self.user_id)}\n{state}"

    def card_files(self) -> list[discord.File]:
        return [
            discord.File(
                render_blackjack_board(self.game, viewer_id=self.user_id),
                filename=self.filename,
            )
        ]

    def refresh(self) -> None:
        try:
            self.display.content = self._text()
        except ValueError:
            self.display.content = "## Blackjack\nYour cards are unavailable."
        self.gallery.items = [discord.MediaGalleryItem(f"attachment://{self.filename}")]
        can_act = not self.game.finished and self.game.can_act(self.user_id)
        self.hit_button.disabled = not can_act
        self.stand_button.disabled = not can_act
        if self.double_button is not None:
            self.double_button.disabled = not (
                can_act and self.game.can_double_for(self.user_id)
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "These private cards belong to another player.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _hit(self, interaction: discord.Interaction) -> None:
        await self.parent._private_action(interaction, "hit", self)

    async def _stand(self, interaction: discord.Interaction) -> None:
        await self.parent._private_action(interaction, "stand", self)

    async def _double(self, interaction: discord.Interaction) -> None:
        await self.parent._private_action(interaction, "double", self)

    async def on_timeout(self) -> None:
        self.stop()


class BlackjackView(discord.ui.LayoutView):
    """Blackjack controls rendered with Components V2.

    Gameplay controls are shared by all participants in a multiplayer round;
    :meth:`interaction_check` gates them to the player whose turn it is.
    Private card details are kept out of the public board while a round is
    live; only each player's up-card and card-back placeholders are shown
    until the round resolves.
    """

    def __init__(
        self,
        game: BlackjackGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
        on_double: DoubleCallback | None = None,
        on_double_player: DoublePlayerCallback | None = None,
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.on_double = on_double
        self.on_double_player = on_double_player
        self.message: discord.Message | None = None
        # Private card panels are kept per participant so gameplay updates can
        # edit the existing ephemeral message rather than sending another one
        # after every action.
        self.private_views: dict[int, BlackjackPrivateView] = {}
        self.display = discord.ui.TextDisplay(self._text())
        self.gallery = discord.ui.MediaGallery()
        self.separator = discord.ui.Separator()
        self.footer = discord.ui.TextDisplay(self._footer_text())
        self.hit_button = discord.ui.Button(
            label="Hit", style=discord.ButtonStyle.secondary
        )
        self.stand_button = discord.ui.Button(
            label="Stand", style=discord.ButtonStyle.primary
        )
        self.double_button: discord.ui.Button | None = None
        self.view_cards_button = discord.ui.Button(
            label="View cards",
            style=discord.ButtonStyle.secondary,
            custom_id="blackjack:view-cards",
        )
        self.hit_button.callback = self._hit
        self.stand_button.callback = self._stand
        controls: list[discord.ui.Button] = [self.hit_button, self.stand_button]
        if self.game.house_player_id is None:
            self.double_button = discord.ui.Button(
                label="Double", style=discord.ButtonStyle.success
            )
            self.double_button.callback = self._double
            controls.append(self.double_button)
        self.view_cards_button.callback = self._view_cards
        if self.game.multiplayer:
            controls.append(self.view_cards_button)
        self.container = discord.ui.Container(
            self.display,
            self.gallery,
            self.separator,
            self.footer,
            discord.ui.ActionRow(*controls),
            accent_color=accent_color,
        )
        self.add_item(self.container)
        self.refresh()

    def _text(self) -> str:
        result = self.game.result
        if self.game.settlement_error:
            return "## Blackjack\nI couldn't settle that wager. Please try again."
        if self.game.timed_out:
            return "## Blackjack\nGame timed out." + (
                " All wagers were forfeited."
                if self.game.stake_total or any(self.game.stake_by_player.values())
                else ""
            )
        if self.game.finished:
            if self.game.player_hands:
                winners = [
                    discord.utils.escape_markdown(
                        self.game.player_names.get(user_id, str(user_id))
                    )
                    for user_id in self.game.participant_ids
                    if self.game.player_results.get(user_id) in {"win", "blackjack"}
                ]
                if winners:
                    title = f"## Blackjack · {', '.join(winners)} " + (
                        "wins!" if len(winners) == 1 else "win!"
                    )
                elif any(
                    self.game.player_results.get(user_id) == "push"
                    for user_id in self.game.participant_ids
                ):
                    title = "## Blackjack · Push"
                else:
                    title = "## Blackjack · No winner"
                rows: list[str] = []
                for user_id in self.game.participant_ids:
                    if (
                        self.game.house_player_id is not None
                        and user_id == self.game.house_player_id
                    ):
                        continue
                    name = discord.utils.escape_markdown(
                        self.game.player_names.get(user_id, str(user_id))
                    )
                    result = self.game.player_results.get(user_id, "loss")
                    payout = self.game.player_payouts.get(user_id, 0)
                    outcome = result.title()
                    rows.append(
                        f"**{name}:** "
                        f"{self.game.total_for(user_id)} · {outcome}"
                        + (f" · **{payout:,} Coins**" if payout else "")
                    )
                lines = [title]
                if self.game.house_player_id is None:
                    lines.append(f"**Dealer:** {self.game.dealer_total}")
                else:
                    house_id = int(self.game.house_player_id)
                    house_name = discord.utils.escape_markdown(
                        self.game.player_names.get(house_id, str(house_id))
                    )
                    house_result = self.game.player_results.get(house_id, "loss")
                    house_payout = self.game.player_payouts.get(house_id, 0)
                    lines.append(
                        f"**Dealer ({house_name}):** {self.game.total_for(house_id)}"
                        f" · {house_result.title()}"
                        + (f" · **{house_payout:,} Coins**" if house_payout else "")
                    )
                lines.extend(rows)
                return "\n".join(lines)
            title = {
                "blackjack": "## Blackjack · You win!",
                "win": "## Blackjack · You win!",
                "push": (
                    "## Blackjack · Push · your wager was returned."
                    if self.game.stake_total
                    else "## Blackjack · Push."
                ),
                "loss": "## Blackjack · You lost.",
            }.get(result or "loss", "## Blackjack · Game over.")
            payout = (
                f"\nPayout: **{self.game.payout:,} Coins**."
                if self.game.stake_total
                else ""
            )
            return (
                f"{title}{payout}\n"
                f"**Dealer:** {self.game.dealer_total}\n"
                f"**You:** {self.game.player_total}"
            )
        if self.game.player_hands:
            current = self.game.current_player_id
            current_name = self.game.player_names.get(current or 0, str(current))
            house_id = self.game.house_player_id
            if house_id is not None:
                house_name = discord.utils.escape_markdown(
                    self.game.player_names.get(house_id, str(house_id))
                )
                upcard = (
                    hand_value([self.game.dealer_hand[0]])
                    if self.game.dealer_hand
                    else None
                )
                hidden_count = max(0, len(self.game.dealer_hand) - 1)
                hidden_cards = " + ".join("Hidden card" for _ in range(hidden_count))
                dealer_line = f"**Dealer ({house_name}):**"
                if upcard is not None:
                    dealer_line += f" {upcard}"
                if hidden_cards:
                    dealer_line += f" + {hidden_cards}"
            else:
                dealer_line = "**Dealer:** Hidden card"
            rows: list[str] = []
            for user_id in self.game.participant_ids:
                if house_id is not None and user_id == house_id:
                    continue
                # Opponents never see another player's private total or
                # cards while a PvP round is live, even after that player
                # stands.  The final board reveals the totals once the round
                # has resolved.
                shown = "Hidden cards"
                name = discord.utils.escape_markdown(
                    self.game.player_names.get(user_id, str(user_id))
                )
                rows.append(f"**{name}:** {shown}")
            actor = self.game.last_actor_id
            actor_name = (
                discord.utils.escape_markdown(
                    self.game.player_names.get(actor, str(actor))
                )
                if actor is not None
                else None
            )
            action = (
                f"{actor_name} {self.game.last_action}"
                if actor_name and self.game.last_action
                else self.game.last_action or "dealing"
            )
            return "\n".join(
                (
                    f"## Blackjack · {discord.utils.escape_markdown(current_name)}'s turn",
                    dealer_line,
                    *rows,
                    f"Last action: **{action}**",
                )
            )
        wager = (
            f"\n**Wager:** {self.game.stake_total:,} Coins"
            if self.game.stake_total
            else ""
        )
        dealer_line = "**Dealer:** Hidden card"
        if self.game.dealer_hand:
            dealer_line = (
                f"**Dealer:** {hand_value([self.game.dealer_hand[0]])} + Hidden card"
            )
        return (
            "## Blackjack\n"
            f"{dealer_line}\n"
            f"**You:** {self.game.player_total}"
            f"{wager}"
        )

    def _footer_text(self) -> str:
        """Return the result footer shown below the board separator."""

        if not self.game.finished:
            # TextDisplay requires a non-empty value.  The zero-width space is
            # intentionally invisible while the round is live; a result is
            # populated here once the game ends.
            return "\u200b"
        if self.game.player_hands:
            lines: list[str] = []
            for user_id in self.game.participant_ids:
                name = discord.utils.escape_markdown(
                    self.game.player_names.get(user_id, str(user_id))
                )
                outcome = self.game.player_results.get(user_id, "loss")
                payout = self.game.player_payouts.get(user_id, 0)
                if outcome in {"win", "blackjack"}:
                    lines.append(
                        f"-# {name} won {payout:,} Coins."
                        if payout
                        else f"-# {name} won."
                    )
                elif outcome == "push":
                    lines.append(
                        f"-# {name} pushed · {payout:,} Coins returned."
                        if payout
                        else f"-# {name} pushed."
                    )
            return "\n".join(lines or ["-# No player won this round."])
        if self.game.stake_total:
            if self.game.result in {"win", "blackjack"}:
                return f"-# You won {self.game.payout:,} Coins."
            if self.game.result == "push":
                return f"-# Your wager was returned ({self.game.payout:,} Coins)."
            return "-# You lost your wager."
        return "\u200b"

    def card_files(self) -> list[discord.File]:
        return [
            discord.File(render_blackjack_board(self.game), filename="blackjack.png")
        ]

    def private_card_files(self, user_id: int) -> list[discord.File]:
        """Return a board image that reveals only ``user_id``'s hand."""

        return [
            discord.File(
                render_blackjack_board(self.game, viewer_id=user_id),
                filename=BlackjackPrivateView.filename,
            )
        ]

    def private_card_text(self, user_id: int) -> str:
        return self.game.private_card_text(user_id)

    def _get_private_view(self, user_id: int) -> BlackjackPrivateView:
        """Get (or create) the participant's private card panel.

        Gameplay actions continue to use the currently registered panel so
        its ephemeral message can be edited in place.  ``send_private_cards``
        intentionally creates a fresh panel for each public ``View cards``
        click; see ``_new_private_view`` below.
        """

        target_id = int(user_id)
        # Validate the participant before constructing the view so an invalid
        # user receives the normal, concise error instead of a rendering error.
        self.game.hand_for(target_id)
        view = self.private_views.get(target_id)
        if view is None or view.is_finished():
            view = BlackjackPrivateView(self, target_id)
            self.private_views[target_id] = view
        else:
            view.refresh()
        return view

    def _new_private_view(self, user_id: int) -> BlackjackPrivateView:
        """Create a fresh private panel for a ``View cards`` interaction.

        Ephemeral interaction responses can become stale after a player hits
        or stands (and Discord may no longer allow editing the original
        webhook message).  Keeping a single cached panel made a later
        ``View cards`` click appear to do nothing.  Retire the old panel and
        register a new one so every click always has a live, actionable view.
        """

        target_id = int(user_id)
        self.game.hand_for(target_id)
        previous = self.private_views.get(target_id)
        if previous is not None:
            previous.stop()
        view = BlackjackPrivateView(self, target_id)
        self.private_views[target_id] = view
        return view

    async def _update_private_views(self) -> None:
        """Refresh every private panel that has already been sent."""

        for private_view in tuple(self.private_views.values()):
            private_view.refresh()
            if private_view.message is None:
                continue
            try:
                await private_view.message.edit(
                    view=private_view,
                    attachments=private_view.card_files(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.HTTPException, discord.NotFound):
                # Ephemeral messages can disappear from Discord before the
                # public game ends.  Every subsequent View cards click creates
                # a fresh panel regardless of this message's state.
                private_view.message = None

    async def _private_action(
        self,
        interaction: discord.Interaction,
        action: Literal["hit", "stand", "double"],
        private_view: BlackjackPrivateView,
    ) -> None:
        """Apply an action from a private panel and sync both messages."""

        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Blackjack game is over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self.game.multiplayer and not self.game.can_act(interaction.user.id):
                current = self.game.current_player_id
                name = self.game.player_names.get(current or 0, str(current))
                await interaction.response.send_message(
                    f"It is **{discord.utils.escape_markdown(name)}'s** turn.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if action == "hit":
                if self.game.multiplayer:
                    self.game.hit_for(interaction.user.id)
                else:
                    self.game.hit()
            elif action == "stand":
                if self.game.multiplayer:
                    self.game.stand_for(interaction.user.id)
                else:
                    self.game.stand()
            else:
                if self.game.house_player_id is not None:
                    await interaction.response.send_message(
                        "Double is not available in player-versus-player Blackjack.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                if not self.game.can_double_for(interaction.user.id):
                    await interaction.response.send_message(
                        "Double is only available on your first two cards.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                if self.game.multiplayer:
                    if self.on_double_player is not None:
                        allowed = await self.on_double_player(
                            self.game, interaction.user.id
                        )
                    elif self.on_double is not None:
                        allowed = await self.on_double(self.game)
                    else:
                        allowed = True
                    if not allowed:
                        await interaction.response.send_message(
                            "You do not have enough Coins to double this wager.",
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                    self.game.double_for(interaction.user.id)
                else:
                    if self.on_double is not None and not await self.on_double(
                        self.game
                    ):
                        await interaction.response.send_message(
                            "You do not have enough Coins to double this wager.",
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                    self.game.double()

            self.refresh()
            private_view.refresh()
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self,
                        attachments=self.card_files(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
            await interaction.response.edit_message(
                view=private_view,
                attachments=private_view.card_files(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if self.game.finished and self.on_finish is not None:
            self.stop()
            await self.on_finish(self.game)
        elif not self.game.finished:
            await self._update_private_views()

    async def send_private_cards(
        self, interaction: discord.Interaction, user_id: int | None = None
    ) -> None:
        """Show a participant's cards in a fresh private CV2 panel."""

        target_id = int(user_id if user_id is not None else interaction.user.id)
        try:
            # Always send a new ephemeral panel when the public button is
            # pressed.  Reusing a previously sent ephemeral response can
            # silently fail after a hit/stand because its interaction token
            # has expired or the old view has already been stopped.
            private_view = self._new_private_view(target_id)
        except ValueError:
            kwargs = {
                "content": "You are not a player in this Blackjack game.",
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(**kwargs)
            else:
                await interaction.response.send_message(**kwargs)
            return

        # A lightweight fallback keeps isolated/unit-test Context mocks (which
        # do not expose Webhook.followup or original_response) compatible. Real
        # Discord interactions always use the Components V2 path below.
        if not hasattr(interaction, "followup") and not hasattr(
            interaction, "original_response"
        ):
            kwargs = {
                "content": self.private_card_text(target_id),
                "files": self.private_card_files(target_id),
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(**kwargs)
            else:
                await interaction.response.send_message(**kwargs)
            return

        kwargs = {
            "view": private_view,
            "files": private_view.card_files(),
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if interaction.response.is_done():
            sent = await interaction.followup.send(wait=True, **kwargs)
        else:
            await interaction.response.send_message(**kwargs)
            try:
                sent = await interaction.original_response()
            except (discord.HTTPException, AttributeError):
                sent = None
        if isinstance(sent, discord.Message):
            private_view.message = sent

    def refresh(self) -> None:
        self.display.content = self._text()
        self.gallery.items = [discord.MediaGalleryItem("attachment://blackjack.png")]
        self.footer.content = self._footer_text()
        self.hit_button.disabled = self.game.finished
        self.stand_button.disabled = self.game.finished
        current = self.game.current_player_id
        if self.double_button is not None:
            self.double_button.disabled = not (
                self.game.can_double_for(current)
                if current is not None
                else self.game.can_double
            )
        # Private card panels are a PvP-only affordance.  Solo/house games
        # show the player's hand directly in the public board and therefore do
        # not expose a button that could create an ephemeral response.
        self.view_cards_button.disabled = not self.game.multiplayer

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.game.participant_ids:
            # Viewing a private hand is always allowed for either participant,
            # even while the other player is taking their turn.  Gameplay
            # controls remain restricted to the active player below.
            data = getattr(interaction, "data", None)
            if (
                isinstance(data, dict)
                and data.get("custom_id") == "blackjack:view-cards"
            ):
                return True
            if not self.game.finished and self.game.multiplayer:
                if self.game.current_player_id != interaction.user.id:
                    current = self.game.current_player_id
                    name = self.game.player_names.get(current or 0, str(current))
                    await interaction.response.send_message(
                        f"It is **{discord.utils.escape_markdown(name)}'s** turn.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return False
            return True
        await interaction.response.send_message(
            (
                "This Blackjack game is over."
                if self.game.finished
                else "This Blackjack game belongs to another user."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _edit(self, interaction: discord.Interaction) -> None:
        self.refresh()
        await interaction.response.edit_message(
            view=self,
            attachments=self.card_files(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _hit(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Blackjack game is over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self.game.multiplayer:
                if not self.game.can_act(interaction.user.id):
                    current = self.game.current_player_id
                    name = self.game.player_names.get(current or 0, str(current))
                    await interaction.response.send_message(
                        f"It is **{discord.utils.escape_markdown(name)}'s** turn.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                self.game.hit_for(interaction.user.id)
            else:
                self.game.hit()
            await self._edit(interaction)
        if self.game.finished and self.on_finish is not None:
            self.stop()
            await self.on_finish(self.game)
        else:
            await self._update_private_views()

    async def _stand(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Blackjack game is over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self.game.multiplayer:
                if not self.game.can_act(interaction.user.id):
                    current = self.game.current_player_id
                    name = self.game.player_names.get(current or 0, str(current))
                    await interaction.response.send_message(
                        f"It is **{discord.utils.escape_markdown(name)}'s** turn.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                self.game.stand_for(interaction.user.id)
            else:
                self.game.stand()
            await self._edit(interaction)
        if self.game.finished and self.on_finish is not None:
            self.stop()
            await self.on_finish(self.game)
        else:
            await self._update_private_views()

    async def _double(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Blackjack game is over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self.game.house_player_id is not None:
                await interaction.response.send_message(
                    "Double is not available in player-versus-player Blackjack.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not self.game.can_double_for(interaction.user.id):
                await interaction.response.send_message(
                    "Double is only available on your first two cards.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self.game.multiplayer:
                if self.on_double_player is not None:
                    allowed = await self.on_double_player(
                        self.game, interaction.user.id
                    )
                elif self.on_double is not None:
                    allowed = await self.on_double(self.game)
                else:
                    allowed = True
                if not allowed:
                    await interaction.response.send_message(
                        "You do not have enough Coins to double this wager.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                self.game.double_for(interaction.user.id)
            else:
                if self.on_double is not None and not await self.on_double(self.game):
                    await interaction.response.send_message(
                        "You do not have enough Coins to double this wager.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                self.game.double()
            await self._edit(interaction)
        if self.game.finished and self.on_finish is not None:
            self.stop()
            await self.on_finish(self.game)
        else:
            await self._update_private_views()

    async def _view_cards(self, interaction: discord.Interaction) -> None:
        if not self.game.multiplayer:
            # This callback is not included in solo views, but gracefully
            # handle stale components from a previous message without sending
            # an ephemeral card panel.
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        await self.send_private_cards(interaction)

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.finished:
                return
            self.game.timeout()
            self.refresh()
            for private_view in self.private_views.values():
                private_view.refresh()
                private_view.stop()
                if private_view.message is not None:
                    try:
                        await private_view.message.edit(
                            view=private_view,
                            attachments=private_view.card_files(),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except (discord.HTTPException, discord.NotFound):
                        private_view.message = None
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self,
                        attachments=self.card_files(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        if self.on_finish is not None:
            await self.on_finish(self.game)


class BlackjackCommands:
    """Text Blackjack command and shared game controller."""

    bot: Fishie
    _blackjack_games: dict[int, BlackjackGame]

    @commands.command(
        name="blackjack",
        aliases=("black-jack", "bj"),
        extras={"usage": "[user] [bid]"},
    )
    async def blackjack(
        self,
        ctx: Context,
        *,
        arguments: str | None = commands.param(
            default=None,
            displayed_name="user/bid",
            description="Optional user and/or Coin bid (in either order).",
        ),
    ) -> None:
        """Play free Blackjack, or challenge a player with an optional wager.

        The Fun cog's dispatcher accepts a target and Coin bid in either
        order.  Keeping this text command variadic lets the same UX work for
        ``blackjack @user 100`` and ``blackjack 100 @user`` while preserving
        the normal no-argument free game.
        """

        entry = getattr(self, "_blackjack_entry", None)
        if callable(entry):
            values = tuple(arguments.split()) if arguments else ()
            await cast(Callable[..., Awaitable[None]], entry)(ctx, *values)
            return
        # The fallback is useful when this mixin is instantiated on its own in
        # a test or lightweight extension.  It only supports a numeric house
        # wager because the full Fun cog owns the dynamic duel parser.
        try:
            parsed = parse_coin_amount(arguments) if arguments else None
            if parsed == EVERYTHING_AMOUNT:
                wallet = await self.bot.currency.get_wallet(ctx.author.id)
                amount = int(wallet.balance)
            else:
                amount = int(parsed) if parsed is not None else None
        except (CoinAmountError, TypeError, ValueError):
            await ctx.send(
                "Give a numeric Coin wager, or use the full Blackjack command "
                "to challenge another player.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._start_blackjack(ctx, amount)

    async def _start_blackjack(self, ctx: Context, amount: int | None = None) -> None:
        checker = getattr(self, "_require_currency_tracking", None)
        if amount and callable(checker):
            allowed = await cast(Callable[[Context], Awaitable[bool]], checker)(ctx)
            if not allowed:
                return
        user_id = int(ctx.author.id)
        active = self._blackjack_games.get(user_id)
        if active is not None and not active.finished:
            await ctx.send(
                "You already have an active Blackjack game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            amount = int(amount or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount and amount < BLACKJACK_MIN_BID:
            await ctx.send(
                f"Coin wagers must be at least **{BLACKJACK_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        wager = None
        if amount:
            try:
                wager = await self.bot.currency.open_wager(
                    user_id, amount, source=BLACKJACK_SOURCE
                )
            except InvalidAmount:
                await ctx.send(
                    f"Coin wagers must be at least **{BLACKJACK_MIN_BID:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except InsufficientFunds as error:
                await ctx.send(
                    f"You only have **{error.balance:,} Coins**, so you can't wager "
                    f"**{error.required:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        game = BlackjackGame.new(
            user_id,
            amount,
            wager.id if wager is not None else 0,
        )
        view = BlackjackView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_blackjack,
            on_double=self._double_blackjack,
        )
        self._blackjack_games[user_id] = game
        try:
            view.message = await ctx.send(
                view=view,
                files=view.card_files(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._blackjack_games.pop(user_id, None)
            view.stop()
            if wager is not None:
                try:
                    await self.bot.currency.settle_wager(wager.id, wager.stake)
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund a Blackjack wager after startup failed"
                    )
            raise
        if game.finished:
            await self._finish_blackjack(game)

    async def _double_blackjack(self, game: BlackjackGame) -> bool:
        if not game.stake:
            return True
        try:
            wager = await self.bot.currency.open_wager(
                game.user_id, game.stake, source=BLACKJACK_SOURCE
            )
        except (InvalidAmount, InsufficientFunds):
            return False
        game.wager_ids.append(wager.id)
        return True

    async def _finish_blackjack(self, game: BlackjackGame) -> None:
        if self._blackjack_games.get(game.user_id) is not game or game.settled:
            return
        self._blackjack_games.pop(game.user_id, None)
        payout = game.settlement_payout()
        if not game.wager_ids:
            game.payout = payout
            game.settled = True
        else:
            try:
                # Split the result across reservations so a doubled win is
                # represented as two winning wagers (rather than marking the
                # second reservation lost while crediting its payout through
                # the first). Integer remainders go to the first wager.
                count = len(game.wager_ids)
                base, remainder = divmod(payout, count)
                for index, wager_id in enumerate(game.wager_ids):
                    await self.bot.currency.settle_wager(
                        wager_id, base + (remainder if index == 0 else 0)
                    )
                game.payout = payout
                game.settled = True
            except Exception:
                game.settlement_error = True
                game.payout = 0
                self.bot.logger.exception("Failed to settle Blackjack wager")
        if game.view is not None:
            game.view.refresh()
            game.view.stop()
            for private_view in game.view.private_views.values():
                private_view.refresh()
                private_view.stop()
                if private_view.message is not None:
                    try:
                        await private_view.message.edit(
                            view=private_view,
                            attachments=private_view.card_files(),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except (discord.HTTPException, discord.NotFound):
                        private_view.message = None
            if game.view.message is not None:
                try:
                    await game.view.message.edit(
                        view=game.view,
                        attachments=game.view.card_files(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

    # The implementation below remains as a compatibility shim for older
    # checkouts; the methods above intentionally shadow it.
    async def _legacy_start_blackjack(self, ctx: Context, amount: int = 100) -> None:
        """Deprecated pre-free-game implementation retained for reference."""
        await self._start_blackjack(ctx, amount)
