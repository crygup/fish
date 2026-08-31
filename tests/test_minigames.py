from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.fun import Fun
from extensions.fun.minigames import (
    COLOR_MEMORIZE_DIFFICULTIES,
    UNSCRAMBLE_WORDS,
    WORD_GAME_WORDS,
    WORD_LIST_PATH,
    ColorMemorizeGame,
    ColorMemorizeView,
    color_memorize_content,
    scramble_word,
)
from extensions.tools.command_stats import CommandStats


def test_minigame_commands_are_registered() -> None:
    names = {command.name for command in Fun.__cog_commands__}
    assert {"dice", "game", "color", "unscramble", "lightsout", "blackjack"} <= names
    wordbomb = next(
        command for command in Fun.__cog_commands__ if command.name == "wordbomb"
    )
    assert set(wordbomb.aliases) == {"wb", "word-bomb"}
    assert {command.name for command in Fun.color.commands} == {"stats"}
    assert getattr(Fun.color, "app_command", None) is None
    assert {command.name for command in Fun.game.commands} == {
        "tic-tac-toe",
        "connect-four",
        "unscramble",
        "color-memorize",
        "rock-paper-scissors",
        "2048",
        "lights-out",
        "wordle",
        "memory",
        "higher-or-lower",
        "heads-or-tails",
        "wordbomb",
        "click",
        "race",
        "luckyroll",
        "slots",
        "dice",
        "8ball",
        "mines",
        "blackjack",
        "crash",
    }

    assert Fun.higher_or_lower.aliases == (
        "hol",
        "higher",
        "lower",
        "higherorlower",
        "highorlow",
        "higherlower",
        "highlow",
    )
    assert Fun.heads_or_tails.aliases == (
        "headsortails",
        "headortail",
        "coinflip",
        "cf",
    )
    assert {command.name for command in Fun.RPSCommand.commands} == {"stats"}
    assert (
        CommandStats.stats.get_command("rps") is CommandStats.stats_rock_paper_scissors
    )


def test_dice_and_color_difficulty_contracts() -> None:
    assert Fun.dice.name == "dice"
    assert Fun.dice.aliases == ("roll",)
    assert COLOR_MEMORIZE_DIFFICULTIES == {
        "easy": (3, 4),
        "normal": (3, 6),
        "hard": (4, 9),
        "extreme": (5, 9),
        "impossible": (6, 10),
    }
    assert getattr(Fun.unscramble, "app_command", None) is None
    for name in (
        "click",
        "dice",
        "twenty_forty_eight",
        "lightsout",
        "higher_or_lower",
        "heads_or_tails",
        "wordle",
        "memory",
        "connectfour",
        "_8ball",
    ):
        assert getattr(getattr(Fun, name), "app_command", None) is None
    difficulty = Fun.game.app_command.get_command("unscramble").get_parameter(
        "difficulty"
    )
    assert difficulty is not None and difficulty.default == "random"


def test_color_memorize_uses_components_and_starts_with_buttons_disabled() -> None:
    game = ColorMemorizeGame(
        user_id=1,
        difficulty="easy",
        palette=("red", "blue", "green"),
        sequence=("red", "blue", "green", "red"),
    )
    assert "🔴" not in color_memorize_content(game)
    cog = SimpleNamespace(bot=SimpleNamespace(embedcolor=discord.Colour.blurple()))
    view = ColorMemorizeView(cast(Any, cog), game)
    assert isinstance(view, discord.ui.LayoutView)
    assert len(view.children) == 1
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    rows = [
        item for item in container.children if isinstance(item, discord.ui.ActionRow)
    ]
    buttons = [
        button
        for row in rows
        for button in row.children
        if isinstance(button, discord.ui.Button)
    ]
    assert len(buttons) == 3
    assert all(button.disabled for button in buttons)


def test_color_memorize_highlights_only_the_current_preview_color() -> None:
    game = ColorMemorizeGame(
        user_id=1,
        difficulty="easy",
        palette=("red", "blue", "green"),
        sequence=("red", "blue", "green", "red"),
        phase="showing",
        highlight="blue",
    )
    cog = SimpleNamespace(bot=SimpleNamespace(embedcolor=discord.Colour.blurple()))
    view = ColorMemorizeView(cast(Any, cog), game)
    assert view.buttons["blue"].style is discord.ButtonStyle.primary
    assert view.buttons["red"].style is discord.ButtonStyle.secondary
    assert view.buttons["green"].style is discord.ButtonStyle.secondary
    assert all(button.disabled for button in view.buttons.values())


def test_color_memorize_marks_every_button_red_after_a_loss() -> None:
    game = ColorMemorizeGame(
        user_id=1,
        difficulty="easy",
        palette=("red", "blue", "green"),
        sequence=("red", "blue", "green", "red"),
        phase="failed",
        result="You lost. That was not the right sequence.",
    )
    cog = SimpleNamespace(bot=SimpleNamespace(embedcolor=discord.Colour.blurple()))
    view = ColorMemorizeView(cast(Any, cog), game)
    assert "You lost" in view.status_display.content
    assert all(
        button.style is discord.ButtonStyle.danger for button in view.buttons.values()
    )
    assert all(button.disabled for button in view.buttons.values())


def test_scramble_is_not_the_original_word() -> None:
    scrambled = scramble_word("photosynthesis")
    assert sorted(scrambled) == sorted("photosynthesis")
    assert scrambled != "photosynthesis"


def test_scramble_uses_the_shared_google_word_list() -> None:
    assert WORD_LIST_PATH.is_file()
    assert len(WORD_GAME_WORDS) > 9900
    assert WORD_GAME_WORDS[0] == "the"
    assert all(word.isalpha() and word.islower() for word in WORD_GAME_WORDS)


def test_scramble_difficulty_buckets_use_appropriate_word_lengths() -> None:
    assert UNSCRAMBLE_WORDS["easy"]
    assert UNSCRAMBLE_WORDS["normal"]
    assert UNSCRAMBLE_WORDS["hard"]
    assert all(4 <= len(word) <= 6 for word in UNSCRAMBLE_WORDS["easy"])
    assert all(7 <= len(word) <= 9 for word in UNSCRAMBLE_WORDS["normal"])
    assert all(len(word) >= 10 for word in UNSCRAMBLE_WORDS["hard"])
