from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import discord

from utils.paths import FILES_ROOT

if TYPE_CHECKING:
    from . import Fun


COLOR_MEMORIZE_EMOJIS = {
    "red": "🔴",
    "blue": "🔵",
    "green": "🟢",
    "yellow": "🟡",
    "orange": "🟠",
    "purple": "🟣",
}

COLOR_MEMORIZE_DIFFICULTIES: dict[str, tuple[int, int]] = {
    "easy": (3, 4),
    "normal": (3, 6),
    "hard": (4, 9),
    "extreme": (5, 9),
    "impossible": (6, 10),
}
# ``word-games.txt`` is the single shared dictionary for Scramble, Word Bomb,
# LastLetter, and future word games. Its two- and three-letter entries were
# reviewed before being checked in; longer entries are retained from the
# larger dictionary. Wordle keeps a separate five-letter answer/guess list.
WORD_GAME_WORD_LIST_PATH = FILES_ROOT / "data" / "word-games.txt"
WORDLE_WORD_LIST_PATH = FILES_ROOT / "data" / "wordle.txt"

# Keep the old path names as source-compatible aliases for extensions that
# import the dictionary location. They point at the canonical files rather
# than preserving the retired data-file split.
WORD_LIST_PATH = WORD_GAME_WORD_LIST_PATH
WORD_BOMB_WORD_LIST_PATH = WORDLE_WORD_LIST_PATH

# Keep a small fallback so the game remains usable if a local data file is
# missing from a development checkout or an older deployment image.
_FALLBACK_WORDS = (
    "apple",
    "beach",
    "bread",
    "chair",
    "cloud",
    "grape",
    "house",
    "lemon",
    "mouse",
    "plant",
    "river",
    "stone",
    "blanket",
    "capture",
    "diamond",
    "journey",
    "lantern",
    "monster",
    "picture",
    "rainbow",
    "season",
    "thunder",
    "treasure",
    "whisper",
    "adventure",
    "architecture",
    "constellation",
    "consequence",
    "encyclopedia",
    "misunderstood",
    "parallelism",
    "photosynthesis",
    "revolutionary",
    "transformation",
    "unpredictable",
    "vulnerability",
)


def _load_word_game_words(path: Path = WORD_LIST_PATH) -> tuple[str, ...]:
    """Load unique, alphabetic words for scramble and future word games."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    words: list[str] = []
    seen: set[str] = set()
    for line in lines:
        word = line.strip().casefold()
        if not word or not word.isalpha() or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return tuple(words) or _FALLBACK_WORDS


def _word_difficulty_buckets(words: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Split the shared list into predictable lengths for each difficulty."""

    buckets = {
        "easy": tuple(word for word in words if 4 <= len(word) <= 6),
        "normal": tuple(word for word in words if 7 <= len(word) <= 9),
        "hard": tuple(word for word in words if len(word) >= 10),
    }
    if all(buckets.values()):
        return buckets

    # A custom or incomplete list should not make a difficulty unusable.
    fallback = _word_difficulty_buckets(_FALLBACK_WORDS)
    return {
        difficulty: buckets[difficulty] or fallback[difficulty]
        for difficulty in buckets
    }


WORD_GAME_WORDS = _load_word_game_words()
UNSCRAMBLE_WORDS = _word_difficulty_buckets(WORD_GAME_WORDS)

# Word Bomb uses the frequency-ranked portion of the shared list plus the
# Wordle dictionary for its full candidate tuples. The rest of the shared
# dictionary is indexed through one representative candidate per fragment so
# importing the game does not allocate a tuple containing every rare match.
WORD_BOMB_WORDS = tuple(
    dict.fromkeys(
        (*WORD_GAME_WORDS[:10_000], *_load_word_game_words(WORDLE_WORD_LIST_PATH))
    )
)
# Keep this name for callers that use the extended-candidate index; the shared
# list now contains those entries directly.
WORD_BOMB_EXTENDED_WORDS = WORD_GAME_WORDS

# Word Bomb uses short fragments rather than a fixed answer list.  Build the
# fragments from the words we already trust for the other word games so every
# fragment selected below has at least one valid answer. Keeping the bundled
# candidate map in memory avoids scanning the smaller lists for every turn;
# the large dictionary contributes only a compact word/fragment index.
WORD_BOMB_CANDIDATES: dict[str, tuple[str, ...]] = {}
for _word in WORD_BOMB_WORDS:
    for _length in (2, 3, 4):
        if len(_word) < _length:
            continue
        for _index in range(len(_word) - _length + 1):
            _fragment = _word[_index : _index + _length]
            current = WORD_BOMB_CANDIDATES.get(_fragment)
            if current is None:
                WORD_BOMB_CANDIDATES[_fragment] = (_word,)
            elif _word not in current:
                WORD_BOMB_CANDIDATES[_fragment] = (*current, _word)

# Keep the large dictionary compact: the game only needs a set of valid words,
# selectable fragments, and one representative candidate per extended
# fragment. Full candidate tuples remain available for the smaller bundled
# lists.
WORD_BOMB_WORD_LOOKUP = set(
    (*WORD_GAME_WORDS, *_load_word_game_words(WORDLE_WORD_LIST_PATH))
)
WORD_BOMB_FRAGMENT_SETS: dict[int, set[str]] = {
    length: {fragment for fragment in WORD_BOMB_CANDIDATES if len(fragment) == length}
    for length in (2, 3, 4)
}
WORD_BOMB_EXTENDED_CANDIDATE_EXAMPLES: dict[str, str] = {}
for _word in WORD_BOMB_EXTENDED_WORDS:
    for _length in (2, 3, 4):
        if len(_word) < _length:
            continue
        for _index in range(len(_word) - _length + 1):
            _fragment = _word[_index : _index + _length]
            WORD_BOMB_FRAGMENT_SETS[_length].add(_fragment)
            WORD_BOMB_EXTENDED_CANDIDATE_EXAMPLES.setdefault(_fragment, _word)

WORD_BOMB_COMMON_WORDS = frozenset(WORD_GAME_WORDS[:1000])
WORD_BOMB_EASY_FRAGMENTS = tuple(
    fragment
    for fragment, candidates in WORD_BOMB_CANDIDATES.items()
    if len(fragment) == 2
    and sum(word in WORD_BOMB_COMMON_WORDS for word in candidates) >= 2
)
WORD_BOMB_CUSTOM_WORDS: set[str] = set()


def normalize_word_bomb_words(words: Iterable[str]) -> tuple[str, ...]:
    """Return unique alphabetic custom words in their canonical form."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in words:
        word = str(value).strip().casefold()
        if len(word) < 2 or not word.isalpha() or word in seen:
            continue
        seen.add(word)
        normalized.append(word)
    return tuple(normalized)


def add_word_bomb_words(words: Iterable[str]) -> tuple[str, ...]:
    """Add custom words to the in-memory candidate index and return new ones."""

    added: list[str] = []
    for word in normalize_word_bomb_words(words):
        if word in WORD_BOMB_WORD_LOOKUP or word in WORD_BOMB_CUSTOM_WORDS:
            continue
        WORD_BOMB_CUSTOM_WORDS.add(word)
        WORD_BOMB_WORD_LOOKUP.add(word)
        added.append(word)
        for length in (2, 3, 4):
            if len(word) < length:
                continue
            for index in range(len(word) - length + 1):
                fragment = word[index : index + length]
                candidates = WORD_BOMB_CANDIDATES.get(fragment, ())
                if word not in candidates:
                    WORD_BOMB_CANDIDATES[fragment] = (*candidates, word)
                WORD_BOMB_FRAGMENT_SETS[length].add(fragment)
                WORD_BOMB_EXTENDED_CANDIDATE_EXAMPLES.setdefault(fragment, word)
    return tuple(added)


def word_bomb_fragments(length: int, *, easy: bool = False) -> tuple[str, ...]:
    """Return selectable fragments of ``length`` with valid word matches.

    The normal phase should use common, approachable fragments.  Restricting
    that pool to fragments found in the first thousand words and with at
    least two common-word matches prevents an isolated combination such as
    ``pv`` (which only occurs in ``pvc``) from appearing at the beginning of a
    game.  Faster phases continue to use the complete candidate map.
    """

    if length not in (2, 3, 4):
        raise ValueError("Word Bomb fragments must be two, three, or four letters")
    fragments = tuple(WORD_BOMB_FRAGMENT_SETS[length])
    if easy and length == 2:
        fragments = WORD_BOMB_EASY_FRAGMENTS
    return fragments


def choose_word_bomb_fragment(length: int, *, easy: bool = False) -> str:
    """Choose a random fragment that occurs in at least one known word."""

    fragments = word_bomb_fragments(length, easy=easy)
    if not fragments:
        raise RuntimeError(f"No Word Bomb fragments are available for length {length}")
    return random.choice(fragments)


def word_bomb_candidates(fragment: str) -> tuple[str, ...]:
    """Return known words containing ``fragment`` (case-insensitive)."""

    normalized_fragment = fragment.casefold()
    bundled = WORD_BOMB_CANDIDATES.get(normalized_fragment, ())
    example = WORD_BOMB_EXTENDED_CANDIDATE_EXAMPLES.get(normalized_fragment)
    extended = (example,) if example is not None else ()
    return tuple(dict.fromkeys((*bundled, *extended)))


def is_valid_word_bomb_guess(guess: str, fragment: str) -> bool:
    """Check that a guess is in the shared word list and contains a fragment."""

    normalized_guess = guess.strip().casefold()
    normalized_fragment = fragment.strip().casefold()
    if (
        normalized_fragment in normalized_guess
        and normalized_guess in WORD_BOMB_WORD_LOOKUP
    ):
        return True

    # Accept common English plural forms without needing to enumerate every
    # inflection in the static word files.  The fragment must still be in the
    # submitted plural, so forms such as ``babies`` do not incorrectly satisfy
    # a fragment that only occurs in ``baby``.
    if normalized_fragment not in normalized_guess or not normalized_guess.endswith(
        "s"
    ):
        return False
    plural_bases: set[str] = set()
    if normalized_guess.endswith("ies"):
        plural_bases.add(normalized_guess[:-3] + "y")
    if normalized_guess.endswith(("ses", "xes", "zes", "ches", "shes")):
        plural_bases.add(normalized_guess[:-2])
    plural_bases.add(normalized_guess[:-1])
    if normalized_guess.endswith("ves"):
        plural_bases.update({normalized_guess[:-3] + "f", normalized_guess[:-3] + "fe"})
    return any(
        normalized_fragment in base and base in WORD_BOMB_WORD_LOOKUP
        for base in plural_bases
    )


def is_valid_word_game_word(word: str) -> bool:
    """Return whether a word is accepted by the shared word-game lists.

    Word Bomb and LastLetter use the same bundled and owner-managed lookup.
    Word Bomb also accepts a small set of regular plural forms when their
    singular form is present, so the standalone check follows that same rule
    using the first two letters as a valid Word Bomb fragment.
    """

    normalized = word.strip().casefold()
    if not normalized or not normalized.isalpha():
        return False
    if normalized in WORD_BOMB_WORD_LOOKUP:
        return True
    if len(normalized) < 2:
        return False
    return is_valid_word_bomb_guess(normalized, normalized[:2])


def is_valid_word_start_guess(guess: str, prefix: str) -> bool:
    """Check a word-game guess that must begin with *prefix*.

    Word Bomb's custom dictionary is stored in ``WORD_BOMB_WORD_LOOKUP`` by
    :func:`add_word_bomb_words`. Reusing the canonical validator here keeps
    LastLetter aligned with both the bundled dictionaries and owner-added
    words, including the supported plural forms.
    """

    normalized_guess = guess.strip().casefold()
    normalized_prefix = prefix.strip().casefold()
    if (
        not normalized_guess
        or not normalized_prefix
        or not normalized_guess.isalpha()
        or not normalized_prefix.isalpha()
        or not normalized_guess.startswith(normalized_prefix)
    ):
        return False
    return is_valid_word_bomb_guess(normalized_guess, normalized_prefix)


async def add_correct_answer_reaction(
    answer: discord.Message | None,
    prompt: discord.Message,
) -> None:
    """React to a correct user's answer, falling back to the bot prompt.

    A missing reaction permission or a deleted message should never abort a
    running game. The answer is preferred so the confirmation is attached to
    the player's actual guess; the prompt remains a useful fallback.
    """

    if answer is not None:
        try:
            await answer.add_reaction("✅")
            return
        except Exception:
            pass
    try:
        await prompt.add_reaction("✅")
    except Exception:
        pass


@dataclass
class ColorMemorizeGame:
    user_id: int
    difficulty: str
    palette: tuple[str, ...]
    sequence: tuple[str, ...]
    message: discord.Message | None = None
    view: discord.ui.LayoutView | None = None
    phase: str = "countdown"
    position: int = 0
    countdown: int | None = None
    highlight: str | None = None
    result: str | None = None
    animation_task: asyncio.Task[None] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass
class UnscrambleGame:
    channel_id: int
    difficulty: str
    word: str
    scrambled: str
    author_id: int = 0
    message: discord.Message | None = None
    view: discord.ui.View | None = None
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class ColorMemorizeView(discord.ui.LayoutView):
    """Components V2 view used to show and answer a color sequence."""

    def __init__(self, cog: Fun, game: ColorMemorizeGame) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game
        self.buttons: dict[str, discord.ui.Button] = {}
        for color in game.palette:
            button = discord.ui.Button(
                label=COLOR_MEMORIZE_EMOJIS[color],
                style=discord.ButtonStyle.secondary,
                custom_id=f"color-memorize:{game.user_id}:{color}",
            )
            button.callback = self._callback(color)
            self.buttons[color] = button

        rows = [
            discord.ui.ActionRow(*buttons)
            for start in range(0, len(self.buttons), 5)
            if (buttons := list(self.buttons.values())[start : start + 5])
        ]
        self.status_display = discord.ui.TextDisplay(color_memorize_content(game))
        self.container = discord.ui.Container(
            self.status_display,
            *rows,
            accent_color=self.cog.bot.embedcolor,
        )
        self.add_item(self.container)
        self.refresh()

    def _callback(self, color: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.game.user_id:
                await interaction.response.send_message(
                    "This color memory game belongs to another user.", ephemeral=True
                )
                return
            if self.game.phase != "input":
                await interaction.response.send_message(
                    "The sequence is still being shown. Please wait.", ephemeral=True
                )
                return
            await self.cog._color_memorize_guess(interaction, self.game, color)

        return callback

    def refresh(self) -> None:
        """Render the current phase and button state into Components V2."""

        self.status_display.content = color_memorize_content(self.game)
        for color, button in self.buttons.items():
            if self.game.phase == "showing":
                button.style = (
                    discord.ButtonStyle.primary
                    if color == self.game.highlight
                    else discord.ButtonStyle.secondary
                )
                button.disabled = True
            elif self.game.phase == "input":
                button.style = discord.ButtonStyle.secondary
                button.disabled = False
            elif self.game.phase == "failed":
                button.style = discord.ButtonStyle.danger
                button.disabled = True
            elif self.game.phase == "won":
                button.style = discord.ButtonStyle.success
                button.disabled = True
            else:
                button.style = discord.ButtonStyle.secondary
                button.disabled = True

    async def on_timeout(self) -> None:
        await self.cog._finish_color_memorize(self.game, won=False, timed_out=True)


class UnscrambleView(discord.ui.View):
    """Let the player reshuffle the active word without starting a new game."""

    def __init__(self, cog: Fun, game: UnscrambleGame) -> None:
        # The game owns the 60-second timeout. Keeping this view alive until the
        # game finishes lets the timeout task update the same message cleanly.
        super().__init__(timeout=None)
        self.cog = cog
        self.game = game
        self.reshuffle_button = discord.ui.Button(
            label="Reshuffle",
            style=discord.ButtonStyle.secondary,
            custom_id=f"unscramble:reshuffle:{game.channel_id}",
        )
        self.reshuffle_button.callback = self._reshuffle
        self.add_item(self.reshuffle_button)

    def disable_all(self) -> None:
        self.reshuffle_button.disabled = True
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.author_id:
            return True
        await interaction.response.send_message(
            "Only the person who started the game can reshuffle the word.",
            ephemeral=True,
        )
        return False

    async def _reshuffle(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.cog._unscramble_games.get(self.game.channel_id) is not self.game:
                self.disable_all()
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            self.game.scrambled = scramble_word(
                self.game.word, avoid=self.game.scrambled
            )
            await interaction.response.edit_message(
                content=unscramble_content(self.game),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def color_memorize_content(game: ColorMemorizeGame) -> str:
    if game.phase == "countdown":
        status = (
            f"Get ready... **{game.countdown}**"
            if game.countdown is not None
            else "Get ready..."
        )
    elif game.phase == "showing":
        status = "Memorize the highlighted buttons."
    elif game.phase == "input":
        status = (
            f"Click the sequence in order: **{game.position}/{len(game.sequence)}**"
        )
    elif game.result:
        status = game.result
    else:
        status = "The game has ended."
    return f"## Color Memorize • {game.difficulty.title()}\n{status}"


def scramble_word(word: str, *, avoid: str | None = None) -> str:
    letters = list(word)
    original = "".join(letters)
    for _ in range(12):
        random.shuffle(letters)
        scrambled = "".join(letters)
        if scrambled != original and scrambled != avoid:
            return scrambled
    rotated = original[1:] + original[:1]
    if rotated != original and rotated != avoid:
        return rotated
    return original


def unscramble_content(game: UnscrambleGame) -> str:
    return (
        f"## Unscramble • {game.difficulty.title()}\n"
        f"Unscramble **`{game.scrambled}`**. Anyone can answer within 60 seconds."
    )
