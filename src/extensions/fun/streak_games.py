"""Endless Higher or Lower and Heads or Tails games."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal

import discord

from utils.paths import FILES_ROOT

GAME_TIMEOUT = 10 * 60
CardGuess = Literal["higher", "lower"]
CoinGuess = Literal["heads", "tails"]
FinishCallback = Callable[["StreakGame", bool], Awaitable[None]]
ProgressCallback = Callable[["StreakGame"], Awaitable[None]]

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

    def __post_init__(self) -> None:
        self.current_card = self.rng.choice(self.deck)

    def guess(self, choice: CardGuess) -> bool:
        """Reveal a different-rank card and return whether the guess was right."""

        if self.finished:
            return False
        choices = tuple(
            card for card in self.deck if card.value != self.current_card.value
        )
        next_card = self.rng.choice(choices)
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

    def guess(self, choice: CoinGuess) -> bool:
        if self.finished:
            return False
        result: CoinGuess = self.rng.choice(("heads", "tails"))
        self.last_guess = choice
        self.last_result = result
        if choice == result:
            self.streak += 1
            return True
        self.finished = True
        return False


class HigherOrLowerView(discord.ui.LayoutView):
    filename = "higher-or-lower.png"

    def __init__(
        self,
        game: HigherOrLowerGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.on_progress = on_progress
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(self._text())
        self.higher = discord.ui.Button(
            label="Higher", style=discord.ButtonStyle.secondary
        )
        self.lower = discord.ui.Button(
            label="Lower", style=discord.ButtonStyle.secondary
        )
        self.higher.callback = self._higher
        self.lower.callback = self._lower
        self.container = discord.ui.Container(
            self.display,
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(f"attachment://{self.filename}")
            ),
            discord.ui.ActionRow(self.higher, self.lower),
            accent_color=accent_color,
        )
        self.add_item(self.container)
        self.refresh()

    def _text(self) -> str:
        if self.game.timed_out:
            return f"## Higher or Lower\nGame timed out at a streak of **{self.game.streak:,}**."
        if self.game.finished:
            card = self.game.revealed_card or self.game.current_card
            return (
                "## Higher or Lower\n"
                f"It was the **{card.label}**. Final streak: **{self.game.streak:,}**."
            )
        return (
            "## Higher or Lower\n"
            f"**Current card:** {self.game.current_card.label}\n"
            f"**Streak:** {self.game.streak:,}\n"
            "Will the next card be higher or lower?"
        )

    def card_file(self) -> discord.File:
        return discord.File(self.game.current_card.path, filename=self.filename)

    def refresh(self) -> None:
        self.display.content = self._text()
        self.higher.disabled = self.game.finished
        self.lower.disabled = self.game.finished

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
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.on_progress = on_progress
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
        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(self.heads, self.tails),
            accent_color=accent_color,
        )
        self.add_item(self.container)
        self.refresh()

    def _text(self) -> str:
        if self.game.timed_out:
            return f"## Heads or Tails\nGame timed out at a streak of **{self.game.streak:,}**."
        if self.game.finished:
            result = (self.game.last_result or "unknown").title()
            return (
                f"## Heads or Tails\nIt landed on **{result}**. "
                f"Final streak: **{self.game.streak:,}**."
            )
        result = ""
        if self.game.last_result is not None:
            result = f"\nLast flip: **{self.game.last_result.title()}**"
        return (
            "## Heads or Tails\n"
            f"**Streak:** {self.game.streak:,}{result}\n"
            "Choose the next result."
        )

    def refresh(self) -> None:
        self.display.content = self._text()
        self.heads.disabled = self.game.finished
        self.tails.disabled = self.game.finished

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id and not self.game.finished:
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
            self.stop()
            if self.on_finish is not None:
                await self.on_finish(self.game, False)

    async def _heads(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "heads")

    async def _tails(self, interaction: discord.Interaction) -> None:
        await self._guess(interaction, "tails")

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
