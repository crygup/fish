"""Solo Mines game and its Components V2 board."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from .tictactoe import EMPTY_CELL_LABEL

if TYPE_CHECKING:
    from extensions.context import Context


BOARD_SIZE = 5
CELL_COUNT = BOARD_SIZE * BOARD_SIZE
MIN_BOMBS = 1
MAX_BOMBS = CELL_COUNT - 1
GAME_TIMEOUT = 10 * 60
HOUSE_EDGE = 0.03
SAFE_EMOJI = "💎"
MINE_EMOJI = "💣"
HIDDEN_EMOJI = EMPTY_CELL_LABEL

FinishCallback = Callable[["MinesGame", str], Awaitable[None]]


@dataclass(slots=True)
class MinesGame:
    """One deterministic board with a wager reserved in the wallet."""

    user_id: int
    stake: int
    wager_id: int
    bomb_count: int
    mines: frozenset[int]
    rng: random.Random | random.SystemRandom = field(
        default_factory=random.SystemRandom, repr=False
    )
    revealed: set[int] = field(default_factory=set)
    multiplier: float = 1.0
    payout: int = 0
    finished: bool = False
    hit_mine: int | None = None
    cashed_out: bool = False
    cleared: bool = False
    timed_out: bool = False
    settled: bool = False
    settlement_error: bool = False
    started_at: datetime = field(default_factory=discord.utils.utcnow)
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    view: MinesView | None = field(default=None, repr=False)

    @classmethod
    def new(
        cls,
        user_id: int,
        stake: int,
        wager_id: int,
        bomb_count: int,
        *,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> MinesGame:
        generator = rng or random.SystemRandom()
        mines = frozenset(generator.sample(range(CELL_COUNT), bomb_count))
        return cls(
            user_id=user_id,
            stake=stake,
            wager_id=wager_id,
            bomb_count=bomb_count,
            mines=mines,
            rng=generator,
        )

    @property
    def safe_count(self) -> int:
        return CELL_COUNT - self.bomb_count

    @property
    def picks(self) -> int:
        return len(self.revealed)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def survival_probability(self, picks: int | None = None) -> float:
        """Return the exact probability of surviving ``picks`` safe draws."""

        count = self.picks if picks is None else int(picks)
        if count < 0 or count > self.safe_count:
            raise ValueError("The number of safe picks is outside the board.")
        probability = 1.0
        for pick in range(count):
            probability *= (self.safe_count - pick) / (CELL_COUNT - pick)
        return probability

    def update_payout(self) -> None:
        if not self.revealed:
            self.multiplier = 1.0
            self.payout = 0
            return
        probability = self.survival_probability()
        # The player receives 97% of the mathematically fair payout.  This
        # matches the supplied examples: five mines pay about 1.21x, 1.53x,
        # and 1.96x after one, two, and three safe picks.
        self.multiplier = (1.0 - HOUSE_EDGE) / probability
        self.payout = max(1, int(self.stake * self.multiplier))

    def reveal(self, index: int) -> str:
        """Reveal a tile and return ``safe``, ``mine``, or ``cleared``."""

        if self.finished:
            raise ValueError("This Mines game is already over.")
        if not 0 <= index < CELL_COUNT:
            raise IndexError("Mines tile index is outside the board.")
        if index in self.revealed:
            raise ValueError("That tile has already been revealed.")
        if index in self.mines:
            self.hit_mine = index
            self.multiplier = 0.0
            self.payout = 0
            self.finished = True
            return "mine"

        self.revealed.add(index)
        self.update_payout()
        if len(self.revealed) == self.safe_count:
            self.cleared = True
            self.finished = True
            return "cleared"
        return "safe"

    def cash_out(self) -> None:
        if self.finished:
            raise ValueError("This Mines game is already over.")
        if not self.revealed:
            raise ValueError("Reveal a safe tile before cashing out.")
        self.update_payout()
        self.cashed_out = True
        self.finished = True

    def timeout(self) -> None:
        if self.finished:
            return
        self.timed_out = True
        self.finished = True
        self.payout = 0
        self.multiplier = 0.0


class MinesView(discord.ui.LayoutView):
    """Author-only 5×5 Mines board with a cash-out action."""

    def __init__(
        self,
        game: MinesGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
    ) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.message: discord.Message | None = None
        self.status = discord.ui.TextDisplay(self._status_text())
        self.buttons: list[discord.ui.Button] = []
        rows: list[discord.ui.ActionRow] = []
        for row in range(BOARD_SIZE):
            row_buttons: list[discord.ui.Button] = []
            for column in range(BOARD_SIZE):
                index = row * BOARD_SIZE + column
                button = discord.ui.Button(
                    label=HIDDEN_EMOJI,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"mines:{game.user_id}:{index}",
                )
                button.callback = self._callback(index)
                self.buttons.append(button)
                row_buttons.append(button)
            rows.append(discord.ui.ActionRow(*row_buttons))
        self.container = discord.ui.Container(
            self.status,
            *rows,
            accent_color=accent_color,
        )
        self.add_item(self.container)
        self.cash_out_button = discord.ui.Button(
            label="Cash out",
            style=discord.ButtonStyle.primary,
            custom_id=f"mines-cash-out:{game.user_id}",
        )
        self.cash_out_button.callback = self._cash_out
        self.add_item(discord.ui.ActionRow(self.cash_out_button))
        self.refresh()

    def _status_text(self) -> str:
        if self.game.settlement_error:
            return "## Mines\nI couldn't settle that wager. Please try again."
        if self.game.hit_mine is not None:
            return f"## Mines\nYou hit a mine and lost **{self.game.stake:,} Coins**."
        if self.game.timed_out:
            return "## Mines\nThe game timed out and your wager was lost."
        if self.game.cashed_out:
            return (
                f"## Mines\nCashed out **{self.game.payout:,} Coins** "
                f"({self.game.multiplier:.2f}×)."
            )
        if self.game.cleared:
            return (
                f"## Mines\nBoard cleared! You won **{self.game.payout:,} Coins** "
                f"({self.game.multiplier:.2f}×)."
            )
        payout = (
            f" · Cash out: **{self.game.payout:,} Coins**" if self.game.revealed else ""
        )
        return (
            "## Mines\n"
            "Reveal safe tiles and cash out before you hit a mine.\n"
            f"-# Bet: **{self.game.stake:,} Coins** · Mines: **{self.game.bomb_count}** "
            f"· Safe picks: **{self.game.picks}**{payout}"
        )

    def refresh(self) -> None:
        self.status.content = self._status_text()
        show_mines = self.game.finished
        for index, button in enumerate(self.buttons):
            is_mine = index in self.game.mines
            revealed = index in self.game.revealed
            visible = revealed or (show_mines and is_mine)
            # Hidden tiles need the braille blank so Discord accepts a button
            # with no visible text.  Once an emoji is shown, keeping that
            # character would add a second invisible glyph to the button's
            # layout and push the emoji off-center.
            button.label = None if visible else HIDDEN_EMOJI
            button.emoji = (MINE_EMOJI if is_mine else SAFE_EMOJI) if visible else None
            if visible and is_mine:
                button.style = discord.ButtonStyle.danger
            elif visible:
                button.style = discord.ButtonStyle.success
            else:
                button.style = discord.ButtonStyle.secondary
            button.disabled = self.game.finished or revealed
        self.cash_out_button.disabled = self.game.finished or not self.game.revealed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id and not self.game.finished:
            return True
        message = (
            "This Mines game is over."
            if self.game.finished
            else "Only the person who started this Mines game can play."
        )
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def _callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            async with self.game.lock:
                if self.game.finished:
                    await interaction.response.send_message(
                        "This Mines game is over.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                try:
                    outcome = self.game.reveal(index)
                except (IndexError, ValueError) as error:
                    await interaction.response.send_message(
                        str(error),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                self.refresh()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            if outcome in {"mine", "cleared"}:
                self.stop()
                if self.on_finish is not None:
                    await self.on_finish(self.game, outcome)

        return callback

    async def _cash_out(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Mines game is already over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not self.game.revealed:
                await interaction.response.send_message(
                    "Reveal a safe tile before cashing out.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self.game.cash_out()
            self.refresh()
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, "cashout")

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.finished:
                return
            self.game.timeout()
            self.refresh()
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, "timeout")


__all__ = (
    "BOARD_SIZE",
    "CELL_COUNT",
    "GAME_TIMEOUT",
    "HOUSE_EDGE",
    "MAX_BOMBS",
    "MIN_BOMBS",
    "MINE_EMOJI",
    "MinesGame",
    "MinesView",
    "SAFE_EMOJI",
)
