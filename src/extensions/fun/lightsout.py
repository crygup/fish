"""Solo five-by-five Lights Out game and Components V2 interface."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

import discord

from .tictactoe import EMPTY_CELL_LABEL

BOARD_SIZE = 5
CELL_COUNT = BOARD_SIZE * BOARD_SIZE
MOVE_TIMEOUT = 10 * 60
OFF_BOARD = (False,) * CELL_COUNT
Board = tuple[bool, ...]


def affected_indices(index: int) -> tuple[int, ...]:
    """Return the pressed cell and its orthogonal neighbors."""

    if not 0 <= index < CELL_COUNT:
        raise IndexError("Lights Out cell index is outside the board")
    row, column = divmod(index, BOARD_SIZE)
    affected = [index]
    if row > 0:
        affected.append(index - BOARD_SIZE)
    if row + 1 < BOARD_SIZE:
        affected.append(index + BOARD_SIZE)
    if column > 0:
        affected.append(index - 1)
    if column + 1 < BOARD_SIZE:
        affected.append(index + 1)
    return tuple(affected)


PRESS_MASKS: tuple[tuple[int, ...], ...] = tuple(
    affected_indices(index) for index in range(CELL_COUNT)
)


def press_board(board: Board, index: int) -> Board:
    """Return a board with one legal Lights Out press applied."""

    if len(board) != CELL_COUNT:
        raise ValueError("A Lights Out board must contain exactly 25 cells")
    if not 0 <= index < CELL_COUNT:
        raise IndexError("Lights Out cell index is outside the board")
    updated = list(board)
    for affected in PRESS_MASKS[index]:
        updated[affected] = not updated[affected]
    return tuple(updated)


def is_solved(board: Board) -> bool:
    return not any(board)


def scramble_board(
    rng: random.Random | random.SystemRandom | None = None,
) -> tuple[Board, tuple[int, ...]]:
    """Build a nonempty, solvable board by applying random legal presses."""

    generator = rng or random.SystemRandom()
    for _ in range(100):
        presses = tuple(generator.sample(range(CELL_COUNT), generator.randint(8, 18)))
        board = OFF_BOARD
        for index in presses:
            board = press_board(board, index)
        if not is_solved(board) and sum(board) >= 5:
            return board, presses

    # A deterministic legal press keeps the fallback solvable and nonempty.
    return press_board(OFF_BOARD, 12), (12,)


@dataclass(slots=True)
class LightsOutGame:
    user_id: int
    guild_id: int | None = None
    channel_id: int | None = None
    board: Board = field(default_factory=lambda: scramble_board()[0])
    scramble: tuple[int, ...] = field(default_factory=tuple, repr=False)
    started_at: datetime = field(default_factory=discord.utils.utcnow)
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    move_count: int = 0
    finished: bool = False
    timed_out: bool = False
    gave_up: bool = False
    completed_duration: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    view: LightsOutView | None = field(default=None, repr=False)

    @classmethod
    def new(
        cls,
        user_id: int,
        *,
        guild_id: int | None = None,
        channel_id: int | None = None,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> LightsOutGame:
        board, scramble = scramble_board(rng)
        return cls(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            board=board,
            scramble=scramble,
        )

    @property
    def duration_seconds(self) -> float:
        if self.completed_duration is not None:
            return self.completed_duration
        return max(0.0, time.monotonic() - self.started_monotonic)

    def press(self, index: int) -> bool:
        """Press one light and return whether that press solved the game."""

        if self.finished:
            return False
        self.board = press_board(self.board, index)
        self.move_count += 1
        if is_solved(self.board):
            self.finished = True
            self.completed_duration = self.duration_seconds
            return True
        return False

    def timeout(self) -> None:
        if self.finished:
            return
        self.completed_duration = self.duration_seconds
        self.timed_out = True
        self.finished = True


FinishCallback = Callable[[LightsOutGame, bool], Awaitable[None]]


class LightsOutView(discord.ui.LayoutView):
    """Author-only Components V2 view containing the full 5x5 light board."""

    def __init__(
        self,
        game: LightsOutGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
    ) -> None:
        super().__init__(timeout=MOVE_TIMEOUT)
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
                    label=EMPTY_CELL_LABEL,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"lights-out:{game.user_id}:{index}",
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
        self.give_up = discord.ui.Button(
            label="Give up",
            style=discord.ButtonStyle.secondary,
            custom_id=f"lights-out-give-up:{game.user_id}",
        )
        self.give_up.callback = self._give_up
        # Keep the session-ending action outside the game board container.
        self.add_item(discord.ui.ActionRow(self.give_up))
        self.refresh()

    def _status_text(self) -> str:
        if self.game.gave_up:
            return "## Lights Out\nYou gave up."
        if self.game.timed_out:
            return "## Lights Out\nThe puzzle timed out."
        if self.game.finished:
            return "## Lights Out\nPuzzle complete! Every light is off."
        return (
            "## Lights Out\n"
            "Turn every light off. Pressing one toggles it and its direct neighbors."
        )

    def refresh(self) -> None:
        self.status.content = self._status_text()
        for index, button in enumerate(self.buttons):
            light_on = self.game.board[index]
            button.label = EMPTY_CELL_LABEL
            button.style = (
                discord.ButtonStyle.success
                if light_on
                else discord.ButtonStyle.secondary
            )
            button.disabled = self.game.finished
        self.give_up.disabled = self.game.finished

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id and not self.game.finished:
            return True
        message = (
            "This Lights Out game is over."
            if self.game.finished
            else "Only the person who started this Lights Out game can play."
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
                        "This Lights Out game is over.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                solved = self.game.press(index)
                self.refresh()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            if solved:
                self.stop()
                if self.on_finish is not None:
                    await self.on_finish(self.game, False)

        return callback

    async def _give_up(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This Lights Out game is already over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self.game.finished = True
            self.game.gave_up = True
            self.game.completed_duration = self.game.duration_seconds
            self.refresh()
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        self.stop()
        if self.on_finish is not None:
            # A give-up is not a completed puzzle, so treat it like a timeout
            # for persistence purposes.
            await self.on_finish(self.game, True)

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
            await self.on_finish(self.game, True)


__all__ = (
    "BOARD_SIZE",
    "CELL_COUNT",
    "LightsOutGame",
    "LightsOutView",
    "OFF_BOARD",
    "affected_indices",
    "is_solved",
    "press_board",
    "scramble_board",
)
