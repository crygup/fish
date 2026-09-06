from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from extensions.fun import Fun
from extensions.fun.lastletter import (
    MAX_GUESSES,
    MAX_PLAYERS,
    MAX_PREFIX_LENGTH,
    LastLetterGame,
    LastLetterLetterView,
    LastLetterPlayer,
    is_valid_lastletter_guess,
)


def test_lastletter_registration_exposes_text_and_hybrid_commands() -> None:
    command = cast(
        Any,
        next(
            command for command in Fun.__cog_commands__ if command.name == "lastletter"
        ),
    )
    assert set(command.aliases) == {"lastl", "last-letter"}
    assert {child.name for child in command.commands} == {"stats"}
    game_command = Fun.game.get_command("last-letter")
    assert game_command is not None
    assert set(game_command.aliases) == {"lastl", "lastletter"}


def test_lastletter_guess_requires_a_dictionary_word_with_the_prefix() -> None:
    assert is_valid_lastletter_guess("apple", "a")
    assert is_valid_lastletter_guess("APPLE", "ap")
    assert not is_valid_lastletter_guess("what", "p")
    assert not is_valid_lastletter_guess("apple!", "a")
    assert not is_valid_lastletter_guess("apple", "z")


def test_lastletter_letter_view_has_four_unique_alphabet_buttons() -> None:
    game = LastLetterGame(
        ctx=cast(Any, SimpleNamespace()),
        channel=cast(Any, SimpleNamespace(id=123)),
        channel_id=123,
        guild_id=None,
        host_id=1,
        players=[LastLetterPlayer(1, "one"), LastLetterPlayer(2, "two")],
        participants=(1, 2),
    )
    view = LastLetterLetterView(
        game,
        game.players[0],
        ("a", "b", "c", "d"),
    )
    assert len(view.buttons) == 4
    assert {button.label for button in view.buttons.values()} == {"A", "B", "C", "D"}
    assert all(
        str(button.custom_id).startswith("lastletter:letter:123:")
        for button in view.buttons.values()
    )


def test_lastletter_progression_increases_after_two_guesses_then_a_successful_roll(
    monkeypatch: Any,
) -> None:
    cog = cast(Any, object.__new__(Fun))
    game = LastLetterGame(
        ctx=cast(Any, SimpleNamespace()),
        channel=cast(Any, SimpleNamespace(id=123)),
        channel_id=123,
        guild_id=None,
        host_id=1,
        players=[LastLetterPlayer(1, "one"), LastLetterPlayer(2, "two")],
        participants=(1, 2),
    )
    assert cog._advance_lastletter_prefix(game, "apple") == "e"
    assert cog._advance_lastletter_prefix(game, "esc") == "c"
    monkeypatch.setattr("extensions.fun.lastletter.random.random", lambda: 0.1)
    assert cog._advance_lastletter_prefix(game, "car") == "ar"
    assert game.prefix_length == 2
    assert game.correct_since_reset == 0
    assert MAX_PREFIX_LENGTH == 4


def test_lastletter_failed_roll_forces_the_next_increase(monkeypatch: Any) -> None:
    cog = cast(Any, object.__new__(Fun))
    game = LastLetterGame(
        ctx=cast(Any, SimpleNamespace()),
        channel=cast(Any, SimpleNamespace(id=123)),
        channel_id=123,
        guild_id=None,
        host_id=1,
        players=[LastLetterPlayer(1, "one"), LastLetterPlayer(2, "two")],
        participants=(1, 2),
    )
    cog._advance_lastletter_prefix(game, "apple")
    cog._advance_lastletter_prefix(game, "esc")
    monkeypatch.setattr("extensions.fun.lastletter.random.random", lambda: 0.9)
    assert cog._advance_lastletter_prefix(game, "car") == "r"
    assert game.force_increase
    assert cog._advance_lastletter_prefix(game, "arson") == "on"
    assert game.prefix_length == 2
    assert not game.force_increase
    assert MAX_GUESSES == 5
    assert MAX_PLAYERS == 10


@pytest.mark.asyncio
async def test_lastletter_counts_only_matching_prefix_messages_toward_five_attempts() -> (
    None
):
    candidates = [
        SimpleNamespace(
            channel=SimpleNamespace(id=123),
            author=SimpleNamespace(id=2, bot=False),
            content=content,
        )
        for content in ("what", "pzzzz", "pnope", "pnope2", "pnope3", "pnope4")
    ]
    wait_for = AsyncMock(side_effect=candidates)
    bot = SimpleNamespace(wait_for=wait_for)
    cog = cast(Any, object.__new__(Fun))
    cog.bot = bot
    channel = cast(Any, SimpleNamespace(id=123, send=AsyncMock()))
    game = LastLetterGame(
        ctx=cast(Any, SimpleNamespace()),
        channel=channel,
        channel_id=123,
        guild_id=None,
        host_id=1,
        players=[LastLetterPlayer(1, "one"), LastLetterPlayer(2, "two")],
        participants=(1, 2),
    )
    prompt = SimpleNamespace()
    channel.send.return_value = prompt

    _prompt, answer = await cog._guess_lastletter_word(game, game.players[1], "p")

    assert answer is None
    assert wait_for.await_count == 6


@pytest.mark.asyncio
async def test_lastletter_pays_one_thousand_per_original_opponent() -> None:
    channel = SimpleNamespace(send=AsyncMock())
    currency = SimpleNamespace(credit=AsyncMock())
    pool = SimpleNamespace(execute=AsyncMock())
    db_cache = SimpleNamespace(
        user_game_tracking_enabled=lambda _user_id: True,
    )
    bot = SimpleNamespace(
        currency=currency,
        pool=pool,
        db_cache=db_cache,
        logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
    )
    cog = cast(Any, object.__new__(Fun))
    cog.bot = bot
    cog._lastletter_games = {}
    players = [LastLetterPlayer(index, str(index)) for index in range(1, 4)]
    game = LastLetterGame(
        ctx=cast(Any, SimpleNamespace()),
        channel=cast(Any, channel),
        channel_id=123,
        guild_id=None,
        host_id=1,
        players=[players[0]],
        participants=(1, 2, 3),
    )
    cog._lastletter_games[123] = game

    await cog._finish_lastletter(game, winner=players[0])

    currency.credit.assert_awaited_once_with(1, 2_000, "lastletter")
    assert pool.execute.await_count == 3
