"""Solo 4x4 memory matching game.

The board state lives in this module so the Discord view remains a thin UI
adapter.  Keeping the matching rules here also makes them straightforward to
exercise without a running Discord client.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

import discord

from .tictactoe import EMPTY_CELL_LABEL

if TYPE_CHECKING:
    from extensions.context import Context


MEMORY_EMOJIS: tuple[str, ...] = (
    "6️⃣",
    "7️⃣",
    "🍔",
    "⭐",
    "🎀",
    "👀",
    "🔥",
    "❤️",
    "🧀",
    "🥀",
    "🎂",
    "👍",
    "🧁",
    "🎉",
    "🌊",
    "🥺",
    "😂",
    "🤣",
    "🫰",
    "🗣️",
    "🐭",
    "🐶",
    "🐱",
    "🐹",
    "🐰",
    "🦊",
    "🐻",
    "🐼",
    "🐻‍❄️",
    "🐨",
    "🦇",
    "🐺",
    "🐯",
    "🦁",
    "🐮",
    "🐷",
    "🐸",
    "🐵",
    "🐧",
    "🐔",
    "🐦",
    "🦆",
    "🦉",
    "🐗",
    "🐴",
    "🦕",
    "🦎",
    "🐍",
    "🐢",
    "🦗",
    "🦥",
    "🐾",
    "🐈",
    "🐈‍⬛",
    "🌻",
    "✨",
    "🍄",
    "🍏",
    "🍎",
    "🍫",
    "🍞",
    "🍓",
    "🍇",
    "🍩",
    "🎲",
    "🎮",
    "🎭",
    "🧩",
    "🩷",
    "🧡",
    "💚",
    "💛",
    "🩵",
    "💙",
    "💜",
    "🖤",
    "🩶",
    "🤍",
    "🤎",
    "💔",
    "🗿",
    "✈️",
    "🧌",
    "🫂",
    "🎃",
    "🤖",
    "👽",
    "💀",
    "🤡",
    "😈",
    "👿",
)


def shuffled_pairs(
    rng: random.Random | random.SystemRandom | None = None,
    emojis: Iterable[str] = MEMORY_EMOJIS,
) -> tuple[str, ...]:
    """Return sixteen positions containing eight unique pairs."""

    generator = rng or random.SystemRandom()
    pool = list(dict.fromkeys(emojis))
    if len(pool) < 8:
        raise ValueError("The memory emoji pool must contain at least eight emojis")
    chosen = generator.sample(pool, 8)
    board = chosen + chosen
    generator.shuffle(board)
    return tuple(board)


@dataclass(slots=True)
class MemoryGame:
    user_id: int
    board: tuple[str, ...] = field(default_factory=shuffled_pairs)
    revealed: set[int] = field(default_factory=set)
    matched: set[int] = field(default_factory=set)
    first: int | None = None
    resolving: bool = False
    finished: bool = False

    def __post_init__(self) -> None:
        if len(self.board) != 16:
            raise ValueError("A memory board must contain sixteen tiles")
        if sorted(self.board.count(value) for value in set(self.board)) != [2] * 8:
            raise ValueError("A memory board must contain eight pairs")

    def select(self, index: int) -> str:
        """Apply one selection and return ``first``, ``match`` or ``second``."""

        if self.finished or self.resolving:
            return "ignored"
        if not 0 <= index < 16 or index in self.matched or index in self.revealed:
            return "ignored"
        self.revealed.add(index)
        if self.first is None:
            self.first = index
            return "first"
        first = self.first
        self.first = None
        if self.board[first] == self.board[index]:
            self.matched.update((first, index))
            if len(self.matched) == 16:
                self.finished = True
            return "match"
        self.resolving = True
        return "second"

    def hide_unmatched(self, first: int, second: int) -> None:
        self.revealed.difference_update((first, second))
        self.resolving = False


class MemoryView(discord.ui.View):
    """Owner-only button board with a short reveal delay for mismatches."""

    def __init__(
        self,
        ctx: Context,
        game: MemoryGame,
        *,
        timeout: float = 600.0,
        on_finish: Callable[[MemoryGame], Awaitable[None]] | None = None,
    ):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.game = game
        self.on_finish = on_finish
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._pending_mismatch: tuple[int, int] | None = None
        self.buttons: list[discord.ui.Button] = []
        self._build()

    def _build(self) -> None:
        for index in range(16):
            button = discord.ui.Button(
                label=EMPTY_CELL_LABEL,
                style=discord.ButtonStyle.secondary,
                row=index // 4,
                custom_id=f"fishie-memory-{index}",
            )
            button.callback = self._callback(index)
            self.buttons.append(button)
            self.add_item(button)
        self.refresh()

    def refresh(self) -> None:
        for index, button in enumerate(self.buttons):
            if index in self.game.revealed or index in self.game.matched:
                button.label = self.game.board[index]
            else:
                button.label = EMPTY_CELL_LABEL
            button.style = (
                discord.ButtonStyle.success
                if index in self.game.matched
                else discord.ButtonStyle.secondary
            )
            button.disabled = (
                self.game.finished or index in self.game.matched or self.game.resolving
            )
        if self.game.finished:
            for button in self.buttons:
                button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id:
            return True
        await interaction.response.send_message(
            "Only the person who started this memory game can play.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def _callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            async with self._lock:
                if self.game.finished or self.game.resolving:
                    await interaction.response.defer()
                    return
                previous = self.game.first
                result = self.game.select(index)
                if result == "ignored":
                    await interaction.response.defer()
                    return
                if result == "second" and previous is not None:
                    self._pending_mismatch = (previous, index)
                self.refresh()
                if self.game.finished:
                    self.stop()
                await interaction.response.edit_message(
                    content=(
                        "## Memory\nYou matched all eight pairs!"
                        if self.game.finished
                        else "## Memory\nMatch all eight pairs."
                    ),
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

                finished = self.game.finished

            if self._pending_mismatch is not None:
                first, second = self._pending_mismatch
                await asyncio.sleep(0.9)
                async with self._lock:
                    self.game.hide_unmatched(first, second)
                    self._pending_mismatch = None
                    self.refresh()
                    if self.message is not None and not self.game.finished:
                        try:
                            await self.message.edit(
                                content="## Memory\nMatch all eight pairs.",
                                view=self,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        except discord.HTTPException:
                            pass
            if finished and self.on_finish is not None:
                await self.on_finish(self.game)

        return callback

    async def on_timeout(self) -> None:
        self.game.finished = True
        self.stop()
        self.refresh()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="## Memory\nThe game timed out.",
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
        if self.on_finish is not None:
            await self.on_finish(self.game)
