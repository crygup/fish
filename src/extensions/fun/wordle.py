"""Wordle game primitives and Components V2 rendering.

The command wiring lives in the ``Fun`` cog.  Keeping the board and result
logic here makes it possible for text-message guesses and modal guesses to
share exactly the same validation path.  No database access is required by
the view itself; :func:`record_wordle_result` is a small persistence helper
for the command layer.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Sequence

import cv2
import discord
import numpy as np

from utils.paths import FILES_ROOT

if TYPE_CHECKING:
    import asyncpg


WORDLE_PATH = FILES_ROOT / "data" / "wordle.txt"
MAX_ATTEMPTS = 6
WORD_LENGTH = 5
WORDLE_TIMEOUT = 600.0

LetterState = Literal["correct", "present", "absent"]
GuessState = tuple[str, tuple[LetterState, ...]]


def load_wordle_words(path: Path = WORDLE_PATH) -> tuple[str, ...]:
    """Load the five-letter words used for both answers and guesses.

    The checked-in list is intentionally treated as data, rather than being
    copied into Python.  Invalid or duplicate lines are ignored so a hand
    edited list cannot make the game crash.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    words: list[str] = []
    seen: set[str] = set()
    for line in lines:
        word = line.strip().casefold()
        if len(word) != WORD_LENGTH or not word.isalpha() or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return tuple(words)


WORDLE_WORDS = load_wordle_words()


def evaluate_guess(answer: str, guess: str) -> tuple[LetterState, ...]:
    """Apply Wordle's two-pass repeated-letter scoring algorithm."""

    answer = answer.casefold()
    guess = guess.casefold()
    if len(answer) != WORD_LENGTH or len(guess) != WORD_LENGTH:
        raise ValueError("Wordle guesses must contain exactly five letters.")

    states: list[LetterState] = ["absent"] * WORD_LENGTH
    remaining = Counter(answer)
    for index, (expected, actual) in enumerate(zip(answer, guess, strict=True)):
        if actual == expected:
            states[index] = "correct"
            remaining[actual] -= 1

    for index, actual in enumerate(guess):
        if states[index] == "correct":
            continue
        if remaining[actual] > 0:
            states[index] = "present"
            remaining[actual] -= 1
    return tuple(states)


def hard_mode_error(history: Sequence[GuessState], guess: str) -> str | None:
    """Return a user-facing hard-mode violation, if any.

    Correct positions are fixed.  Every letter revealed as present/correct
    must occur in the next guess at least as many times as it was revealed.
    This handles repeated letters without imposing the incorrect rule that
    an absent duplicate must always be omitted.
    """

    guess = guess.casefold()
    for previous, states in history:
        for index, state in enumerate(states):
            if state == "correct" and guess[index] != previous[index]:
                return (
                    f"Position {index + 1} must contain **{previous[index].upper()}**."
                )

        required = Counter(
            letter
            for letter, state in zip(previous, states, strict=True)
            if state in {"correct", "present"}
        )
        supplied = Counter(guess)
        for letter, count in required.items():
            if supplied[letter] < count:
                return f"Your guess must contain **{letter.upper()}**."
    return None


@dataclass(slots=True)
class WordleGame:
    """Mutable state for one user's game in one channel."""

    user_id: int
    channel_id: int
    answer: str
    guild_id: int | None = None
    hard_mode: bool = False
    colourblind_mode: bool = False
    started_at: datetime = field(default_factory=discord.utils.utcnow)
    guesses: list[GuessState] = field(default_factory=list)
    result: Literal["won", "lost"] | None = None
    message: discord.Message | None = None
    view: discord.ui.LayoutView | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def attempts(self) -> int:
        return len(self.guesses)

    @property
    def completed(self) -> bool:
        return self.result is not None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (discord.utils.utcnow() - self.started_at).total_seconds())

    def submit(self, raw_guess: str, words: set[str] | frozenset[str]) -> GuessState:
        """Validate and record a guess, returning its letter states."""

        guess = raw_guess.strip().casefold()
        if self.completed:
            raise ValueError("This Wordle game is already over.")
        if len(guess) != WORD_LENGTH or not guess.isalpha():
            raise ValueError("Your guess must be exactly five letters.")
        if guess not in words:
            raise ValueError("That is not a word in my Wordle list.")
        if self.hard_mode:
            error = hard_mode_error(self.guesses, guess)
            if error:
                raise ValueError(error)

        states = evaluate_guess(self.answer, guess)
        entry = (guess, states)
        self.guesses.append(entry)
        if guess == self.answer:
            self.result = "won"
        elif len(self.guesses) >= MAX_ATTEMPTS:
            self.result = "lost"
        return entry


def new_wordle_game(
    *,
    user_id: int,
    channel_id: int,
    guild_id: int | None = None,
    answer: str | None = None,
    hard_mode: bool = False,
    colourblind_mode: bool = False,
    rng: random.Random | None = None,
) -> WordleGame:
    """Create a game using a random answer from :data:`WORDLE_WORDS`."""

    words = WORDLE_WORDS
    if not words:
        raise RuntimeError("The Wordle word list is empty.")
    chooser = rng or random
    selected = answer.casefold() if answer else chooser.choice(words)
    if selected not in words:
        raise ValueError("The Wordle answer must be in the configured word list.")
    return WordleGame(
        user_id=user_id,
        channel_id=channel_id,
        guild_id=guild_id,
        answer=selected,
        hard_mode=hard_mode,
        colourblind_mode=colourblind_mode,
    )


def _state_colour(state: LetterState, colourblind: bool) -> tuple[int, int, int]:
    # The colourblind palette is supplied as #RRGGBB values. OpenCV uses BGR,
    # so the channels are reversed here. Present means the letter is in the
    # wrong spot and correct means it is in the correct spot.
    if colourblind:
        return {
            "correct": (58, 121, 245),  # #f5793a
            "present": (249, 192, 133),  # #85c0f9
            "absent": (80, 80, 80),
        }[state]
    return {
        "correct": (80, 145, 70),
        "present": (0, 190, 220),
        "absent": (70, 70, 70),
    }[state]


def render_wordle_board(game: WordleGame) -> BytesIO:
    """Render a Wordle board to a PNG suitable for a Discord attachment."""

    cell = 86
    gap = 10
    margin_x = 34
    margin_y = 42
    width = margin_x * 2 + WORD_LENGTH * cell + (WORD_LENGTH - 1) * gap
    height = margin_y * 2 + MAX_ATTEMPTS * cell + (MAX_ATTEMPTS - 1) * gap + 56
    image = np.full((height, width, 3), (31, 33, 36), dtype=np.uint8)

    # The OpenCV default font is intentionally used here.  Guesses are ASCII
    # letters, so it renders consistently in the container without relying on
    # a host-specific font file.
    for row in range(MAX_ATTEMPTS):
        y = margin_y + row * (cell + gap)
        guess = game.guesses[row] if row < len(game.guesses) else None
        for column in range(WORD_LENGTH):
            x = margin_x + column * (cell + gap)
            if guess is None:
                fill = (51, 54, 58)
                letter = ""
            else:
                word, states = guess
                fill = _state_colour(states[column], game.colourblind_mode)
                letter = word[column].upper()
            cv2.rectangle(image, (x, y), (x + cell, y + cell), fill, thickness=-1)
            if letter:
                (text_width, text_height), baseline = cv2.getTextSize(
                    letter, cv2.FONT_HERSHEY_SIMPLEX, 1.6, 3
                )
                cv2.putText(
                    image,
                    letter,
                    (
                        x + (cell - text_width) // 2,
                        # ``putText`` receives the baseline rather than the
                        # top-left corner. Include the baseline in the
                        # bounding box so the glyph is vertically centered.
                        y + (cell - text_height - baseline) // 2 + text_height + 3,
                    ),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.6,
                    (255, 255, 255),
                    3,
                    cv2.LINE_AA,
                )
    encoded, data = cv2.imencode(".png", image)
    if not encoded:
        raise RuntimeError("Could not render the Wordle board.")
    return BytesIO(data.tobytes())


def wordle_status_text(game: WordleGame) -> str:
    """Build the text shown alongside the board attachment."""

    if game.result == "won":
        return f"Solved in **{game.attempts}/{MAX_ATTEMPTS}** guesses."
    if game.result == "lost":
        return f"The word was **{game.answer.upper()}**."
    return f"Guess **{game.attempts + 1}/{MAX_ATTEMPTS}**."


GuessCallback = Callable[[discord.Interaction, WordleGame, str], Awaitable[None]]
GiveUpCallback = Callable[[discord.Interaction, WordleGame], Awaitable[None]]


class WordleGuessModal(discord.ui.Modal, title="Enter a Wordle guess"):
    guess = discord.ui.TextInput(
        label="Five-letter guess",
        placeholder="Enter a word",
        min_length=WORD_LENGTH,
        max_length=WORD_LENGTH,
        required=True,
    )

    def __init__(self, game: WordleGame, callback: GuessCallback) -> None:
        super().__init__()
        self.game = game
        self.callback_ref = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.callback_ref(interaction, self.game, str(self.guess.value))


class WordleBoardView(discord.ui.LayoutView):
    """Components V2 board with a modal button.

    ``on_guess`` is supplied by the cog so modal submissions and message
    listeners use the same game completion and persistence code.
    """

    def __init__(
        self,
        game: WordleGame,
        on_guess: GuessCallback,
        *,
        timeout: float = WORDLE_TIMEOUT,
        on_timeout: Callable[[WordleGame], Awaitable[None]] | None = None,
        on_give_up: GiveUpCallback | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.game = game
        self.on_guess = on_guess
        self.on_timeout_callback = on_timeout
        self.on_give_up_callback = on_give_up
        self.board_file = discord.File(render_wordle_board(game), filename="wordle.png")
        self.display = discord.ui.TextDisplay(self._text())
        self.guess_button = discord.ui.Button(
            label="Enter guess", style=discord.ButtonStyle.secondary
        )
        self.guess_button.callback = self._open_modal
        self.give_up_button = discord.ui.Button(
            label="Give up", style=discord.ButtonStyle.danger
        )
        self.give_up_button.callback = self._give_up
        self.container = discord.ui.Container(
            self.display,
            discord.ui.MediaGallery(
                discord.MediaGalleryItem("attachment://wordle.png")
            ),
            discord.ui.ActionRow(self.guess_button, self.give_up_button),
            accent_color=None,
        )
        self.add_item(self.container)
        self.refresh()

    def _text(self) -> str:
        mode = []
        if self.game.hard_mode:
            mode.append("Hard mode")
        elif self.game.colourblind_mode:
            mode.append("Colourblind mode")
        mode_text = f" · {' · '.join(mode)}" if mode else ""
        return f"## Wordle{mode_text}\n{wordle_status_text(self.game)}"

    def refresh(self) -> None:
        self.display.content = self._text()
        self.guess_button.disabled = self.game.completed
        self.give_up_button.disabled = self.game.completed
        self.board_file = discord.File(
            render_wordle_board(self.game), filename="wordle.png"
        )

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message(
                "This Wordle game belongs to another user.", ephemeral=True
            )
            return
        if self.game.completed:
            await interaction.response.send_message(
                "This Wordle game is already over.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            WordleGuessModal(self.game, self.on_guess)
        )

    async def _give_up(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message(
                "This Wordle game belongs to another user.", ephemeral=True
            )
            return
        if self.game.completed:
            await interaction.response.send_message(
                "This Wordle game is already over.", ephemeral=True
            )
            return
        if self.on_give_up_callback is not None:
            await self.on_give_up_callback(interaction, self.game)
            return

        # A standalone view may be used by callers outside the Fun cog.  Keep
        # the button useful there too, while the cog callback handles result
        # persistence for normal and work games.
        self.game.result = "lost"
        self.stop()
        self.refresh()
        await interaction.response.edit_message(
            view=self,
            attachments=[self.board_file],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        self.game.result = self.game.result or "lost"
        self.guess_button.disabled = True
        self.give_up_button.disabled = True
        if self.on_timeout_callback is not None:
            await self.on_timeout_callback(self.game)


class WordleSettingsView(discord.ui.LayoutView):
    """Small per-user settings view for hard and colourblind modes."""

    def __init__(
        self,
        user_id: int,
        hard_mode: bool,
        colourblind_mode: bool,
        on_change: Callable[[int, bool, bool], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.hard_mode = hard_mode
        self.colourblind_mode = colourblind_mode
        self.on_change = on_change
        self.status = discord.ui.TextDisplay(self._text())
        self.hard_button = discord.ui.Button(
            label="Hard mode", style=discord.ButtonStyle.secondary
        )
        self.colour_button = discord.ui.Button(
            label="Colourblind mode", style=discord.ButtonStyle.secondary
        )
        self.hard_button.callback = self._toggle_hard
        self.colour_button.callback = self._toggle_colourblind
        self.add_item(
            discord.ui.Container(
                self.status,
                discord.ui.ActionRow(self.hard_button, self.colour_button),
            )
        )

    def _text(self) -> str:
        return (
            "## Wordle settings\n"
            f"Hard mode: **{'On' if self.hard_mode else 'Off'}**\n"
            f"Colourblind mode: **{'On' if self.colourblind_mode else 'Off'}**"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "These Wordle settings belong to another user.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _toggle_hard(self, interaction: discord.Interaction) -> None:
        self.hard_mode = not self.hard_mode
        await self.on_change(self.user_id, self.hard_mode, self.colourblind_mode)
        self.status.content = self._text()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _toggle_colourblind(self, interaction: discord.Interaction) -> None:
        self.colourblind_mode = not self.colourblind_mode
        await self.on_change(self.user_id, self.hard_mode, self.colourblind_mode)
        self.status.content = self._text()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def message_guess(message: discord.Message, game: WordleGame) -> str | None:
    """Return a valid-shaped message guess for the game's owner only."""

    if message.author.id != game.user_id or message.channel.id != game.channel_id:
        return None
    content = message.content.strip().casefold()
    if len(content) != WORD_LENGTH or not content.isalpha():
        return None
    return content


async def record_wordle_result(
    connection: "asyncpg.Connection | asyncpg.Pool",
    game: WordleGame,
) -> None:
    """Persist a completed game using the migration's simple history table."""

    if game.result is None:
        raise ValueError("Only completed Wordle games can be recorded.")
    await connection.execute(
        """
        INSERT INTO wordle_games
            (user_id, guild_id, channel_id, answer, solved, attempts,
             hard_mode, colourblind_mode, duration_seconds, started_at, finished_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
        """,
        game.user_id,
        game.guild_id,
        game.channel_id,
        game.answer,
        game.result == "won",
        game.attempts,
        game.hard_mode,
        game.colourblind_mode,
        int(round(game.duration_seconds)),
        game.started_at,
    )
    if game.result == "won":
        attempt_column = f"attempt_{game.attempts}"
        await connection.execute(
            f"""
            INSERT INTO wordle_stats (user_id, wins, total_playtime_seconds, {attempt_column})
            VALUES ($1, 1, $2, 1)
            ON CONFLICT (user_id) DO UPDATE SET
                wins = wordle_stats.wins + 1,
                total_playtime_seconds = wordle_stats.total_playtime_seconds + EXCLUDED.total_playtime_seconds,
                {attempt_column} = wordle_stats.{attempt_column} + 1,
                updated_at = now()
            """,
            game.user_id,
            int(round(game.duration_seconds)),
        )
    else:
        await connection.execute(
            """
            INSERT INTO wordle_stats (user_id, losses, total_playtime_seconds)
            VALUES ($1, 1, $2)
            ON CONFLICT (user_id) DO UPDATE SET
                losses = wordle_stats.losses + 1,
                total_playtime_seconds = wordle_stats.total_playtime_seconds + EXCLUDED.total_playtime_seconds,
                updated_at = now()
            """,
            game.user_id,
            int(round(game.duration_seconds)),
        )


async def get_wordle_settings(
    connection: "asyncpg.Connection | asyncpg.Pool", user_id: int
) -> tuple[bool, bool]:
    """Read hard/colourblind settings, defaulting to both disabled."""

    row = await connection.fetchrow(
        "SELECT wordle_hard_mode, wordle_colourblind_mode "
        "FROM user_settings WHERE user_id = $1",
        user_id,
    )
    if row is None:
        return False, False
    return bool(row["wordle_hard_mode"]), bool(row["wordle_colourblind_mode"])


async def save_wordle_settings(
    connection: "asyncpg.Connection | asyncpg.Pool",
    user_id: int,
    hard_mode: bool,
    colourblind_mode: bool,
) -> None:
    await connection.execute(
        """
        INSERT INTO user_settings (user_id, wordle_hard_mode, wordle_colourblind_mode)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET
            wordle_hard_mode = EXCLUDED.wordle_hard_mode,
            wordle_colourblind_mode = EXCLUDED.wordle_colourblind_mode
        """,
        user_id,
        hard_mode,
        colourblind_mode,
    )


__all__ = [
    "MAX_ATTEMPTS",
    "WORDLE_PATH",
    "WORDLE_TIMEOUT",
    "WORDLE_WORDS",
    "GuessState",
    "GiveUpCallback",
    "WordleBoardView",
    "WordleGame",
    "WordleSettingsView",
    "evaluate_guess",
    "get_wordle_settings",
    "hard_mode_error",
    "load_wordle_words",
    "message_guess",
    "new_wordle_game",
    "record_wordle_result",
    "render_wordle_board",
    "save_wordle_settings",
    "wordle_status_text",
]
