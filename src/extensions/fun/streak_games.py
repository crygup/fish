"""Endless Higher or Lower and Heads or Tails games."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal

import discord

from core.currency import MAX_WAGER_PAYOUT
from utils.paths import FILES_ROOT

GAME_TIMEOUT = 10 * 60
GAME_MIN_BID = 10
# Compatibility export for extensions that import the historical limit;
# wager validation now uses the wallet balance rather than an artificial cap.
GAME_MAX_BID = 10_000
CardGuess = Literal["higher", "lower"]
CoinGuess = Literal["heads", "tails"]
RPSChoice = Literal["rock", "paper", "scissors"]
RPSOutcome = Literal["win", "loss", "draw"]
RPS_LOSING_CHOICE: dict[RPSChoice, RPSChoice] = {
    "rock": "scissors",
    "paper": "rock",
    "scissors": "paper",
}
FinishCallback = Callable[["StreakGame", bool], Awaitable[None]]
ProgressCallback = Callable[["StreakGame"], Awaitable[None]]
RestartCallback = Callable[["HeadsOrTailsGame"], Awaitable[bool]]
CashOutCallback = Callable[["HigherOrLowerGame"], Awaitable[bool]]

RANK_VALUES = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "jack": 11,
    "queen": 12,
    "king": 13,
    "ace": 14,
}


def calculate_wager_payout(stake: int, streak: int, limit: int) -> int:
    """Return the complete payout after a streak of wins.

    A wager is debited when the game starts, so this is the amount credited at
    cash-out rather than an amount added to the original stake. The first win
    pays 1.1x, the second 1.2x, and so on. Coins are whole numbers, therefore
    values are rounded down and capped at the configured payout limit.
    """

    if stake < 0 or streak < 0 or limit < 0:
        raise ValueError("Wager payout inputs cannot be negative.")
    if limit == 0 or stake >= limit:
        return min(stake, limit)

    # Apply each round's multiplier to the payout produced by the previous
    # round.  Keeping the calculation integral (rounding down after every
    # round) makes the displayed and settled Coin amounts deterministic.
    payout = stake
    for win_number in range(1, streak + 1):
        payout = (payout * (10 + win_number)) // 10
        if payout >= limit:
            return limit
    return payout


def calculate_constant_wager_payout(
    stake: int, streak: int, limit: int, *, numerator: int = 12, denominator: int = 10
) -> int:
    """Return a payout compounded by one fixed multiplier per win.

    Higher or Lower uses a 1.2x multiplier after every successful prediction.
    The other streak games retain the historical incremental schedule handled
    by :func:`calculate_wager_payout`.
    """

    if stake < 0 or streak < 0 or limit < 0:
        raise ValueError("Wager payout inputs cannot be negative.")
    if denominator <= 0 or numerator < 0:
        raise ValueError("Wager payout multiplier must be non-negative.")
    if limit == 0 or stake >= limit:
        return min(stake, limit)

    payout = stake
    for _ in range(streak):
        payout = (payout * numerator) // denominator
        if payout >= limit:
            return limit
    return payout


@dataclass(frozen=True, slots=True)
class PlayingCard:
    path: Path
    suit: str
    rank: str
    value: int

    @property
    def label(self) -> str:
        return f"{self.rank.title()} of {self.suit.title()}"


def quarter_size_cards() -> tuple[PlayingCard, ...]:
    """Return the bundled 25 percent playing-card deck."""

    root = FILES_ROOT / "images" / "cards"
    cards: list[PlayingCard] = []
    for path in sorted(root.glob("*/25/*.png")):
        suit = path.parent.parent.name.casefold()
        prefix = f"{suit}_"
        if not path.stem.casefold().startswith(prefix):
            continue
        rank = path.stem.casefold()[len(prefix) :]
        value = RANK_VALUES.get(rank)
        if value is not None:
            cards.append(PlayingCard(path, suit, rank, value))
    if len(cards) != 52:
        raise RuntimeError("The bundled 25 percent playing-card deck is incomplete.")
    return tuple(cards)


@dataclass(slots=True)
class StreakGame:
    user_id: int
    streak: int = 0
    finished: bool = False
    timed_out: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    view: discord.ui.LayoutView | None = field(default=None, repr=False)


@dataclass(slots=True)
class HigherOrLowerGame(StreakGame):
    deck: tuple[PlayingCard, ...] = field(default_factory=quarter_size_cards)
    rng: random.Random | random.SystemRandom = field(
        default_factory=random.SystemRandom, repr=False
    )
    current_card: PlayingCard = field(init=False)
    revealed_card: PlayingCard | None = None
    last_guess: CardGuess | None = None
    wager_id: int | None = None
    stake: int = 0
    potential_payout: int = 0
    payout_limit: int = MAX_WAGER_PAYOUT
    payout_capped: bool = False
    cashed_out: bool = False
    cash_out_error: str | None = None

    def __post_init__(self) -> None:
        self.current_card = self.rng.choice(self.deck)
        if self.wager_id is not None:
            if self.stake <= 0:
                raise ValueError("A wager stake must be positive.")
            if self.payout_limit < self.stake:
                raise ValueError("The payout limit cannot be lower than the stake.")
            self.potential_payout = self.stake

    @property
    def has_wager(self) -> bool:
        return self.wager_id is not None

    def _increase_payout(self) -> None:
        """Apply a 1.2x multiplier for every successful prediction."""

        if not self.has_wager or self.payout_capped:
            return
        payout = calculate_constant_wager_payout(
            self.stake, self.streak, self.payout_limit
        )
        if payout >= self.payout_limit:
            self.potential_payout = self.payout_limit
            self.payout_capped = True
        else:
            self.potential_payout = payout

    def guess(self, choice: CardGuess) -> bool:
        """Reveal a different-rank card and return whether the guess was right."""

        if self.finished:
            return False
        choices = tuple(
            card for card in self.deck if card.value != self.current_card.value
        )
        next_card = self.rng.choice(choices)
        # ``random.choice`` always honors the filtered sequence, but keeping
        # this guard makes the invariant explicit and protects against custom
        # RNG implementations used by callers/tests returning an invalid
        # card.  Consecutive cards must never have the same rank.
        if next_card.value == self.current_card.value:
            next_card = choices[0]
        correct = (
            next_card.value > self.current_card.value
            if choice == "higher"
            else next_card.value < self.current_card.value
        )
        self.last_guess = choice
        self.revealed_card = next_card
        self.current_card = next_card
        if correct:
            self.streak += 1
            self._increase_payout()
        else:
            self.finished = True
        return correct


@dataclass(slots=True)
class HeadsOrTailsGame(StreakGame):
    rng: random.Random | random.SystemRandom = field(
        default_factory=random.SystemRandom, repr=False
    )
    last_guess: CoinGuess | None = None
    last_result: CoinGuess | None = None
    wager_id: int | None = None
    stake: int = 0
    potential_payout: int = 0
    payout_limit: int = MAX_WAGER_PAYOUT
    payout_capped: bool = False
    cashed_out: bool = False
    cash_out_error: str | None = None

    def __post_init__(self) -> None:
        if self.wager_id is not None:
            if self.stake <= 0:
                raise ValueError("A wager stake must be positive.")
            if self.payout_limit < self.stake:
                raise ValueError("The payout limit cannot be lower than the stake.")
            self.potential_payout = self.stake

    @property
    def has_wager(self) -> bool:
        return self.wager_id is not None

    def _increase_payout(self) -> None:
        if not self.has_wager or self.payout_capped:
            return
        payout = calculate_wager_payout(self.stake, self.streak, self.payout_limit)
        if payout >= self.payout_limit:
            self.potential_payout = self.payout_limit
            self.payout_capped = True
        else:
            self.potential_payout = payout

    def restart(self) -> None:
        """Reset the game for a new streak after a loss."""

        self.streak = 0
        self.finished = False
        self.timed_out = False
        self.last_guess = None
        self.last_result = None

    def guess(self, choice: CoinGuess) -> bool:
        if self.finished:
            return False
        result: CoinGuess = self.rng.choice(("heads", "tails"))
        self.last_guess = choice
        self.last_result = result
        if choice == result:
            self.streak += 1
            self._increase_payout()
            return True
        self.finished = True
        return False


@dataclass(slots=True)
class RockPaperScissorsGame(StreakGame):
    always_win: bool = False
    rng: random.Random | random.SystemRandom = field(
        default_factory=random.SystemRandom, repr=False
    )
    last_choice: RPSChoice | None = None
    last_bot_choice: RPSChoice | None = None
    last_outcome: RPSOutcome | None = None
    wager_id: int | None = None
    stake: int = 0
    potential_payout: int = 0
    payout_limit: int = MAX_WAGER_PAYOUT
    payout_capped: bool = False
    cashed_out: bool = False
    cash_out_error: str | None = None

    def __post_init__(self) -> None:
        if self.wager_id is not None:
            if self.stake <= 0:
                raise ValueError("A wager stake must be positive.")
            if self.payout_limit < self.stake:
                raise ValueError("The payout limit cannot be lower than the stake.")
            self.potential_payout = self.stake

    @property
    def has_wager(self) -> bool:
        return self.wager_id is not None

    def _increase_payout(self) -> None:
        if self.wager_id is None or self.payout_capped:
            return
        payout = calculate_wager_payout(self.stake, self.streak, self.payout_limit)
        if payout >= self.payout_limit:
            self.potential_payout = self.payout_limit
            self.payout_capped = True
        else:
            self.potential_payout = payout

    def guess(self, choice: RPSChoice) -> RPSOutcome:
        if self.finished:
            return "loss"
        if self.always_win:
            bot_choice: RPSChoice = RPS_LOSING_CHOICE[choice]
        else:
            bot_choice = self.rng.choice(("rock", "paper", "scissors"))
        if choice == bot_choice:
            outcome: RPSOutcome = "draw"
        elif (choice, bot_choice) in {
            ("rock", "scissors"),
            ("paper", "rock"),
            ("scissors", "paper"),
        }:
            outcome = "win"
            self.streak += 1
            self._increase_payout()
        else:
            outcome = "loss"
            self.finished = True
        self.last_choice = choice
        self.last_bot_choice = bot_choice
        self.last_outcome = outcome
        return outcome


class HigherOrLowerView(discord.ui.LayoutView):
    filename = "higher-or-lower.png"

    def __init__(
        self,
        game: HigherOrLowerGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_cash_out: CashOutCallback | None = None,
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.on_progress = on_progress
        self.on_cash_out = on_cash_out
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(self._text())
        self.higher = discord.ui.Button(
            label="Higher", style=discord.ButtonStyle.secondary
        )
        self.lower = discord.ui.Button(
            label="Lower", style=discord.ButtonStyle.secondary
        )
        self.cash_out = discord.ui.Button(
            label="Cash Out", style=discord.ButtonStyle.success
        )
        self.higher.callback = self._higher
        self.lower.callback = self._lower
        self.cash_out.callback = self._cash_out
        action_row = discord.ui.ActionRow(self.higher, self.lower)
        if self.game.has_wager:
            action_row.add_item(self.cash_out)
        self.container = discord.ui.Container(
            self.display,
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(f"attachment://{self.filename}")
            ),
            action_row,
            accent_color=accent_color,
        )
        self.add_item(self.container)
        self.refresh()

    def _text(self) -> str:
        if self.game.timed_out:
            text = (
                "## Higher or Lower\n"
                f"Game timed out at a streak of **{self.game.streak:,}**."
            )
            if self.game.has_wager:
                text += f"\nYou forfeited your **{self.game.stake:,} Coin** wager."
            return text
        if self.game.cashed_out:
            return (
                "## Higher or Lower\n"
                f"Cashed out **{self.game.potential_payout:,} Coins** at a streak "
                f"of **{self.game.streak:,}**."
            )
        if self.game.finished:
            card = self.game.revealed_card or self.game.current_card
            text = (
                "## Higher or Lower\n"
                f"It was the **{card.label}**. Final streak: **{self.game.streak:,}**."
            )
            if self.game.has_wager:
                text += (
                    f"\nYou lost your **{self.game.stake:,} Coin** wager and "
                    f"**{self.game.potential_payout:,} Coin** potential payout."
                )
            return text
        text = (
            "## Higher or Lower\n"
            f"**Current card:** {self.game.current_card.label}\n"
            f"**Streak:** {self.game.streak:,}\n"
        )
        if self.game.has_wager:
            maximum = " (maximum)" if self.game.payout_capped else ""
            text += (
                f"**Wager:** {self.game.stake:,} Coins\n"
                f"**Cash-out value:** {self.game.potential_payout:,} Coins{maximum}\n"
            )
        return text + "Will the next card be higher or lower?"

    def card_file(self) -> discord.File:
        return discord.File(self.game.current_card.path, filename=self.filename)

    def refresh(self) -> None:
        self.display.content = self._text()
        self.higher.disabled = self.game.finished
        self.lower.disabled = self.game.finished
        # A wager cannot be cashed out before the player has made at least one
        # correct guess.  If they abandon the game or time out at streak zero,
        # the normal finish path forfeits the reserved stake.
        self.cash_out.disabled = self.game.finished or self.game.streak < 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id and not self.game.finished:
            return True
        await interaction.response.send_message(
            (
                "This Higher or Lower game is over."
                if self.game.finished
                else "This Higher or Lower game belongs to another user."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _guess(self, interaction: discord.Interaction, choice: CardGuess) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Higher or Lower game is over.", ephemeral=True
                )
                return
            correct = self.game.guess(choice)
            self.refresh()
            await interaction.response.edit_message(
                view=self,
                attachments=[self.card_file()],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if correct and self.on_progress is not None:
            await self.on_progress(self.game)
        if not correct:
            self.stop()
            if self.on_finish is not None:
                await self.on_finish(self.game, False)

    async def _higher(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "higher")

    async def _lower(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "lower")

    async def _cash_out(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Higher or Lower game is over.", ephemeral=True
                )
                return
            if not self.game.has_wager or self.on_cash_out is None:
                await interaction.response.send_message(
                    "This game does not have a Coin wager.", ephemeral=True
                )
                return
            if self.game.streak < 1:
                await interaction.response.send_message(
                    "Make at least one correct guess before cashing out. "
                    "Your wager is forfeited if you leave or time out.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not await self.on_cash_out(self.game):
                await interaction.response.send_message(
                    self.game.cash_out_error
                    or "I couldn't cash out that wager. Please try again.",
                    ephemeral=True,
                )
                return
            self.game.cashed_out = True
            self.game.finished = True
            self.refresh()
            await interaction.response.edit_message(
                view=self,
                attachments=[self.card_file()],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, False)

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.finished:
                return
            self.game.finished = True
            self.game.timed_out = True
            self.refresh()
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self, allowed_mentions=discord.AllowedMentions.none()
                    )
                except discord.HTTPException:
                    pass
        if self.on_finish is not None:
            await self.on_finish(self.game, True)


class HeadsOrTailsView(discord.ui.LayoutView):
    def __init__(
        self,
        game: HeadsOrTailsGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_restart: RestartCallback | None = None,
        on_cash_out: Callable[[HeadsOrTailsGame], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.on_progress = on_progress
        self.on_restart = on_restart
        self.on_cash_out = on_cash_out
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(self._text())
        self.heads = discord.ui.Button(
            label="Heads", style=discord.ButtonStyle.secondary
        )
        self.tails = discord.ui.Button(
            label="Tails", style=discord.ButtonStyle.secondary
        )
        self.heads.callback = self._heads
        self.tails.callback = self._tails
        self.cash_out = discord.ui.Button(
            label="Cash Out", style=discord.ButtonStyle.success
        )
        self.cash_out.callback = self._cash_out
        action_row = discord.ui.ActionRow(self.heads, self.tails)
        if self.game.has_wager:
            action_row.add_item(self.cash_out)
        self.container = discord.ui.Container(
            self.display,
            action_row,
            accent_color=accent_color,
        )
        self.add_item(self.container)
        self.restart = discord.ui.Button(
            label="Restart", style=discord.ButtonStyle.secondary
        )
        self.restart.callback = self._restart
        # Keep the post-loss action separate from the guessing controls.
        if not self.game.has_wager:
            self.add_item(discord.ui.ActionRow(self.restart))
        self.refresh()

    def _text(self) -> str:
        if self.game.timed_out:
            text = f"## Heads or Tails\nGame timed out at a streak of **{self.game.streak:,}**."
            if self.game.has_wager:
                text += f"\nYou forfeited your **{self.game.stake:,} Coin** wager."
            return text
        if self.game.cashed_out:
            return (
                "## Heads or Tails\n"
                f"Cashed out **{self.game.potential_payout:,} Coins** at a streak "
                f"of **{self.game.streak:,}**."
            )
        if self.game.finished:
            result = (self.game.last_result or "unknown").title()
            text = (
                f"## Heads or Tails\nIt landed on **{result}**. "
                f"Final streak: **{self.game.streak:,}**."
            )
            if self.game.has_wager:
                text += (
                    f"\nYou lost your **{self.game.stake:,} Coin** wager and "
                    f"**{self.game.potential_payout:,} Coin** potential payout."
                )
            return text
        result = ""
        if self.game.last_result is not None:
            result = f"\nLast flip: **{self.game.last_result.title()}**"
        text = "## Heads or Tails\n" f"**Streak:** {self.game.streak:,}{result}\n"
        if self.game.has_wager:
            maximum = " (maximum)" if self.game.payout_capped else ""
            text += (
                f"**Wager:** {self.game.stake:,} Coins\n"
                f"**Cash-out value:** {self.game.potential_payout:,} Coins{maximum}\n"
            )
        return text + "Choose the next result."

    def refresh(self) -> None:
        self.display.content = self._text()
        self.heads.disabled = self.game.finished
        self.tails.disabled = self.game.finished
        self.cash_out.disabled = self.game.finished
        self.restart.disabled = not self.game.finished

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id:
            return True
        await interaction.response.send_message(
            (
                "This Heads or Tails game is over."
                if self.game.finished
                else "This Heads or Tails game belongs to another user."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _guess(self, interaction: discord.Interaction, choice: CoinGuess) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Heads or Tails game is over.", ephemeral=True
                )
                return
            correct = self.game.guess(choice)
            self.refresh()
            await interaction.response.edit_message(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        if correct and self.on_progress is not None:
            await self.on_progress(self.game)
        if not correct:
            if self.on_finish is not None:
                await self.on_finish(self.game, False)

    async def _heads(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "heads")

    async def _tails(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "tails")

    async def _cash_out(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Heads or Tails game is over.", ephemeral=True
                )
                return
            if not self.game.has_wager or self.on_cash_out is None:
                await interaction.response.send_message(
                    "This game does not have a Coin wager.", ephemeral=True
                )
                return
            if not await self.on_cash_out(self.game):
                await interaction.response.send_message(
                    self.game.cash_out_error
                    or "I couldn't cash out that wager. Please try again.",
                    ephemeral=True,
                )
                return
            self.game.cashed_out = True
            self.game.finished = True
            self.refresh()
            await interaction.response.edit_message(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, False)

    async def _restart(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if not self.game.finished:
                await interaction.response.send_message(
                    "This Heads or Tails game is still in progress.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self.on_restart is not None and not await self.on_restart(self.game):
                await interaction.response.send_message(
                    "You already have another Heads or Tails game in progress.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self.game.restart()
            self.refresh()
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.finished:
                return
            self.game.finished = True
            self.game.timed_out = True
            self.refresh()
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self, allowed_mentions=discord.AllowedMentions.none()
                    )
                except discord.HTTPException:
                    pass
        if self.on_finish is not None:
            await self.on_finish(self.game, True)


class RockPaperScissorsWagerView(discord.ui.LayoutView):
    def __init__(
        self,
        game: RockPaperScissorsGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_cash_out: Callable[[RockPaperScissorsGame], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.on_progress = on_progress
        self.on_cash_out = on_cash_out
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(self._text())
        self.rock = discord.ui.Button(
            label="Rock", emoji="🪨", style=discord.ButtonStyle.secondary
        )
        self.paper = discord.ui.Button(
            label="Paper", emoji="📄", style=discord.ButtonStyle.secondary
        )
        self.scissors = discord.ui.Button(
            label="Scissors", emoji="✂️", style=discord.ButtonStyle.secondary
        )
        self.cash_out = discord.ui.Button(
            label="Cash Out", style=discord.ButtonStyle.success
        )
        self.rock.callback = self._rock
        self.paper.callback = self._paper
        self.scissors.callback = self._scissors
        self.cash_out.callback = self._cash_out
        action_row = discord.ui.ActionRow(self.rock, self.paper, self.scissors)
        if self.game.has_wager:
            action_row.add_item(self.cash_out)
        self.container = discord.ui.Container(
            self.display, action_row, accent_color=accent_color
        )
        self.add_item(self.container)
        self.refresh()

    def _text(self) -> str:
        if self.game.timed_out:
            text = (
                "## Rock Paper Scissors\n"
                f"Game timed out at a streak of **{self.game.streak:,}**."
            )
            if self.game.has_wager:
                text += f"\nYou forfeited your **{self.game.stake:,} Coin** wager."
            return text
        if self.game.cashed_out:
            return (
                "## Rock Paper Scissors\n"
                f"Cashed out **{self.game.potential_payout:,} Coins** at a streak "
                f"of **{self.game.streak:,}**."
            )
        if self.game.finished:
            text = (
                "## Rock Paper Scissors\n"
                f"You chose **{(self.game.last_choice or 'unknown').title()}** and "
                f"Fishie chose **{(self.game.last_bot_choice or 'unknown').title()}**. "
                f"You lost at a streak of **{self.game.streak:,}**."
            )
            if self.game.has_wager:
                text += (
                    f"\nYou forfeited your **{self.game.stake:,} Coin** wager and "
                    f"**{self.game.potential_payout:,} Coin** potential payout."
                )
            return text

        previous_round = ""
        if self.game.last_outcome is not None:
            user_choice = (self.game.last_choice or "unknown").title()
            bot_choice = (self.game.last_bot_choice or "unknown").title()
            outcome = {
                "win": "You won the last round.",
                "draw": "The last round was a draw.",
                "loss": "You lost the last round.",
            }[self.game.last_outcome]
            previous_round = (
                f"{outcome} You chose **{user_choice}** and Fishie chose "
                f"**{bot_choice}**.\n"
            )
        text = (
            "## Rock Paper Scissors\n"
            f"{previous_round}"
            f"**Streak:** {self.game.streak:,}\n"
        )
        if self.game.has_wager:
            maximum = " (maximum)" if self.game.payout_capped else ""
            text += (
                f"**Wager:** {self.game.stake:,} Coins\n"
                f"**Cash-out value:** {self.game.potential_payout:,} Coins{maximum}\n"
            )
        return text + "Choose rock, paper, or scissors."

    def refresh(self) -> None:
        self.display.content = self._text()
        for button in (self.rock, self.paper, self.scissors, self.cash_out):
            button.disabled = self.game.finished
        # A wager cannot be cashed out before the player has made at least
        # one move.  A draw still counts as a move (and leaves the game
        # active), while an untouched game must keep the cash-out action
        # unavailable.
        self.cash_out.disabled = self.game.finished or self.game.last_choice is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id and not self.game.finished:
            return True
        await interaction.response.send_message(
            (
                "This Rock Paper Scissors game is over."
                if self.game.finished
                else "This Rock Paper Scissors game belongs to another user."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _guess(self, interaction: discord.Interaction, choice: RPSChoice) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Rock Paper Scissors game is over.", ephemeral=True
                )
                return
            outcome = self.game.guess(choice)
            self.refresh()
            await interaction.response.edit_message(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        if outcome == "win" and self.on_progress is not None:
            await self.on_progress(self.game)
        if outcome == "loss":
            self.stop()
            if self.on_finish is not None:
                await self.on_finish(self.game, False)

    async def _rock(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "rock")

    async def _paper(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "paper")

    async def _scissors(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "scissors")

    async def _cash_out(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Rock Paper Scissors game is over.", ephemeral=True
                )
                return
            if not self.game.has_wager or self.on_cash_out is None:
                await interaction.response.send_message(
                    self.game.cash_out_error
                    or "I couldn't cash out that wager. Please try again.",
                    ephemeral=True,
                )
                return
            if self.game.last_choice is None:
                await interaction.response.send_message(
                    "Make a move before cashing out your wager.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not await self.on_cash_out(self.game):
                await interaction.response.send_message(
                    self.game.cash_out_error
                    or "I couldn't cash out that wager. Please try again.",
                    ephemeral=True,
                )
                return
            self.game.cashed_out = True
            self.game.finished = True
            self.refresh()
            await interaction.response.edit_message(
                view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, False)

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.finished:
                return
            self.game.finished = True
            self.game.timed_out = True
            self.refresh()
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self, allowed_mentions=discord.AllowedMentions.none()
                    )
                except discord.HTTPException:
                    pass
        if self.on_finish is not None:
            await self.on_finish(self.game, True)
