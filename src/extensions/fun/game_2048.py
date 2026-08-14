"""The 2048 solo game.

This module intentionally keeps the game rules independent from the Fun cog so
the command can persist scores without having to duplicate (or trust) any UI
state.  The view is a small Components V2 wrapper around that state and sends
the board as a PNG attachment instead of using Discord emoji tiles.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

import cv2
import discord
import numpy as np

if TYPE_CHECKING:
    from extensions.context import Context


BOARD_SIZE = 4
MOVE_TIMEOUT = 10 * 60
Direction = Literal["left", "right", "up", "down"]
VALID_DIRECTIONS: tuple[Direction, ...] = ("left", "right", "up", "down")


def empty_board() -> list[list[int]]:
    """Return a fresh, empty 4x4 board."""

    return [[0 for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def _empty_cells(board: list[list[int]]) -> list[tuple[int, int]]:
    return [
        (row, column)
        for row in range(BOARD_SIZE)
        for column in range(BOARD_SIZE)
        if not board[row][column]
    ]


def spawn_tile(
    board: list[list[int]], rng: random.Random | random.SystemRandom | None = None
) -> tuple[int, int, int] | None:
    """Place a standard 2048 tile and return its location/value.

    A 2 is spawned 90 percent of the time and a 4 the remaining 10 percent.
    ``None`` is returned when the board has no empty cells.
    """

    available = _empty_cells(board)
    if not available:
        return None
    generator = rng or random
    row, column = generator.choice(available)
    value = 4 if generator.random() < 0.1 else 2
    board[row][column] = value
    return row, column, value


def new_board(
    rng: random.Random | random.SystemRandom | None = None,
) -> list[list[int]]:
    """Create a new board with the two starting tiles."""

    board = empty_board()
    spawn_tile(board, rng)
    spawn_tile(board, rng)
    return board


def slide_line(line: list[int]) -> tuple[list[int], int, bool]:
    """Slide one line toward the left and merge each tile at most once."""

    compact = [value for value in line if value]
    merged: list[int] = []
    score = 0
    index = 0
    while index < len(compact):
        value = compact[index]
        if index + 1 < len(compact) and compact[index + 1] == value:
            value *= 2
            score += value
            index += 2
        else:
            index += 1
        merged.append(value)
    result = merged + [0] * (len(line) - len(merged))
    return result, score, result != line


def move_board(
    board: list[list[int]], direction: Direction | str
) -> tuple[list[list[int]], int, bool]:
    """Return ``(board, score_delta, moved)`` after one directional move.

    The input is never mutated and no random tile is spawned here.  Separating
    the deterministic movement from spawning keeps the rules easy to test and
    allows callers to render a preview before committing a move.
    """

    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"Unknown 2048 direction: {direction}")

    result = empty_board()
    score = 0
    moved = False
    for index in range(BOARD_SIZE):
        if direction in ("left", "right"):
            source = list(board[index])
            if direction == "right":
                source.reverse()
            line, line_score, line_moved = slide_line(source)
            if direction == "right":
                line.reverse()
            result[index] = line
        else:
            source = [board[row][index] for row in range(BOARD_SIZE)]
            if direction == "down":
                source.reverse()
            line, line_score, line_moved = slide_line(source)
            if direction == "down":
                line.reverse()
            for row, value in enumerate(line):
                result[row][index] = value
        score += line_score
        moved = moved or line_moved
    return result, score, moved


def has_moves(board: list[list[int]]) -> bool:
    """Return whether at least one legal move remains."""

    if _empty_cells(board):
        return True
    for row in range(BOARD_SIZE):
        for column in range(BOARD_SIZE):
            value = board[row][column]
            if row + 1 < BOARD_SIZE and board[row + 1][column] == value:
                return True
            if column + 1 < BOARD_SIZE and board[row][column + 1] == value:
                return True
    return False


def game_over(board: list[list[int]]) -> bool:
    return not has_moves(board)


def highest_tile(board: list[list[int]]) -> int:
    return max((value for row in board for value in row), default=0)


@dataclass
class Game2048:
    """Mutable per-user state for one 2048 session."""

    owner_id: int
    board: list[list[int]] = field(default_factory=new_board)
    score: int = 0
    started_at: datetime = field(default_factory=discord.utils.utcnow)
    guild_id: int | None = None
    channel_id: int | None = None
    finished: bool = False
    timed_out: bool = False
    gave_up: bool = False
    move_history: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def high_score(self) -> int:
        return self.score

    @property
    def move_count(self) -> int:
        return len(self.move_history)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (discord.utils.utcnow() - self.started_at).total_seconds())

    def move(
        self,
        direction: Direction | str,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> bool:
        """Apply a move and spawn one tile only when the board changed."""

        if self.finished:
            return False
        updated, score, moved = move_board(self.board, direction)
        if not moved:
            return False
        self.board = updated
        self.score += score
        self.move_history.append(str(direction))
        spawn_tile(self.board, rng)
        if game_over(self.board):
            self.finished = True
        return True


TILE_COLORS: dict[int, tuple[int, int, int]] = {
    0: (205, 193, 180),
    2: (238, 228, 218),
    4: (237, 224, 200),
    8: (242, 177, 121),
    16: (245, 149, 99),
    32: (246, 124, 95),
    64: (246, 94, 59),
    128: (237, 207, 114),
    256: (237, 204, 97),
    512: (237, 200, 80),
    1024: (237, 197, 63),
    2048: (237, 194, 46),
}


def render_2048(
    board: list[list[int]],
    score: int = 0,
    *,
    width: int = 640,
    height: int = 720,
) -> BytesIO:
    """Render a 2048 board as a PNG suitable for a Components V2 gallery."""

    width = max(320, int(width))
    height = max(360, int(height))
    image = np.full((height, width, 3), (239, 248, 250), dtype=np.uint8)
    margin = max(12, width // 40)
    header = max(52, height // 9)
    gap = max(6, width // 64)
    board_size = min(width - margin * 2, height - header - margin * 2)
    tile_size = (board_size - gap * (BOARD_SIZE - 1)) // BOARD_SIZE
    board_width = tile_size * BOARD_SIZE + gap * (BOARD_SIZE - 1)
    board_left = (width - board_width) // 2
    board_top = header + margin

    text_color = (101, 110, 119)
    cv2.putText(
        image,
        "2048",
        (margin, margin + 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.35,
        text_color,
        3,
        cv2.LINE_AA,
    )
    score_text = f"Score {score:,}"
    (score_width, score_height), _ = cv2.getTextSize(
        score_text, cv2.FONT_HERSHEY_SIMPLEX, 0.75, 2
    )
    cv2.putText(
        image,
        score_text,
        (width - margin - score_width, margin + score_height),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        text_color,
        2,
        cv2.LINE_AA,
    )
    cv2.rectangle(
        image,
        (board_left - gap, board_top - gap),
        (board_left + board_width + gap, board_top + board_width + gap),
        (160, 173, 187),
        thickness=-1,
    )

    for row in range(BOARD_SIZE):
        for column in range(BOARD_SIZE):
            value = (
                board[row][column]
                if row < len(board) and column < len(board[row])
                else 0
            )
            x = board_left + column * (tile_size + gap)
            y = board_top + row * (tile_size + gap)
            color = TILE_COLORS.get(value, (50, 58, 60))
            cv2.rectangle(
                image,
                (x, y),
                (x + tile_size, y + tile_size),
                color,
                thickness=-1,
            )
            if not value:
                continue
            text = str(value)
            font_scale = max(0.55, tile_size / (210 if value < 1000 else 260))
            thickness = max(1, int(font_scale * 3))
            (text_width, text_height), baseline = cv2.getTextSize(
                text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
            )
            tile_text_color = (101, 110, 119) if value in (2, 4) else (242, 246, 249)
            cv2.putText(
                image,
                text,
                (
                    x + (tile_size - text_width) // 2,
                    y + (tile_size + text_height) // 2 - baseline,
                ),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale,
                tile_text_color,
                thickness,
                cv2.LINE_AA,
            )

    output = BytesIO()
    encoded, data = cv2.imencode(".png", image)
    if not encoded:
        raise RuntimeError("Could not render the 2048 board.")
    output.write(data.tobytes())
    output.seek(0)
    return output


FinishCallback = Callable[[Game2048, bool], Awaitable[None]]


class TwentyFortyEightView(discord.ui.LayoutView):
    """Components V2 controls for a :class:`Game2048` session.

    ``on_finish`` receives ``timed_out=False`` for a normal completed game and
    ``timed_out=True`` when the ten-minute view timeout fires.  This lets the
    caller persist the high score in both cases while only counting playtime
    for normal completions.
    """

    filename = "2048.png"

    def __init__(
        self,
        game: Game2048,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
    ) -> None:
        super().__init__(timeout=MOVE_TIMEOUT)
        self.game = game
        self.on_finish = on_finish
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(self._display_text())
        self.gallery = discord.ui.MediaGallery(
            discord.MediaGalleryItem(f"attachment://{self.filename}")
        )
        self.footer = discord.ui.TextDisplay(self._footer_text())
        self.left = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
        self.up = discord.ui.Button(label="∧", style=discord.ButtonStyle.secondary)
        self.down = discord.ui.Button(label="∨", style=discord.ButtonStyle.secondary)
        self.right = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.left.callback = self._left
        self.up.callback = self._up
        self.down.callback = self._down
        self.right.callback = self._right
        self.give_up = discord.ui.Button(
            label="Give up", style=discord.ButtonStyle.secondary
        )
        self.give_up.callback = self._give_up
        self.container = discord.ui.Container(
            self.display,
            self.gallery,
            discord.ui.ActionRow(self.left, self.up, self.down, self.right),
            discord.ui.Separator(),
            self.footer,
            accent_color=accent_color,
        )
        self.add_item(self.container)
        # Keep the session-ending action outside the game content container.
        self.add_item(discord.ui.ActionRow(self.give_up))
        self.refresh()

    def _display_text(self) -> str:
        if self.game.gave_up:
            return "## 2048\nYou gave up."
        if self.game.timed_out:
            return "## 2048\nGame timed out."
        if self.game.finished:
            return "## 2048\nGame over."
        return "## 2048\nUse the arrows to move the tiles."

    def _footer_text(self) -> str:
        return f"Score: **{self.game.score:,}** · Best tile: **{highest_tile(self.game.board):,}**"

    def refresh(self) -> None:
        self.display.content = self._display_text()
        self.footer.content = self._footer_text()
        for button in (self.left, self.up, self.down, self.right):
            button.disabled = self.game.finished
        self.give_up.disabled = self.game.finished

    def render_file(self) -> discord.File:
        return discord.File(
            render_2048(self.game.board, self.game.score), filename=self.filename
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.owner_id and not self.game.finished:
            return True
        message = (
            "This 2048 game is over."
            if self.game.finished
            else "This 2048 game belongs to another user."
        )
        await interaction.response.send_message(message, ephemeral=True)
        return False

    async def _move(
        self, interaction: discord.Interaction, direction: Direction
    ) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This 2048 game is over.", ephemeral=True
                )
                return
            if not self.game.move(direction):
                await interaction.response.send_message(
                    "That move would not change the board.", ephemeral=True
                )
                return
            self.refresh()
            await interaction.response.edit_message(
                view=self,
                attachments=[self.render_file()],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            finished = self.game.finished
        if finished:
            self.stop()
            if self.on_finish is not None:
                await self.on_finish(self.game, False)

    async def _left(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, "left")

    async def _up(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, "up")

    async def _down(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, "down")

    async def _right(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, "right")

    async def _give_up(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.finished:
                await interaction.response.send_message(
                    "This 2048 game is already over.", ephemeral=True
                )
                return
            self.game.finished = True
            self.game.gave_up = True
            self.refresh()
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, True)

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
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, True)


# A short alias is useful for callers that do not want to spell out the game
# name, while keeping the descriptive class available to type checkers.
Game2048View = TwentyFortyEightView
