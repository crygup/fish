from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from extensions.fun.minigames import (
    WORD_BOMB_CANDIDATES,
    WORD_BOMB_EXTENDED_WORDS,
    WORD_BOMB_WORDS,
    choose_word_bomb_fragment,
    is_valid_word_bomb_guess,
    word_bomb_candidates,
    word_bomb_fragments,
)
from extensions.fun.wordbomb import WordBombGame, WordBombLobbyView, WordBombPlayer
from utils.emojis import bomb, red_bomb, skull_bomb


def test_word_bomb_emojis_are_available() -> None:
    assert str(bomb) == "💣"
    assert str(red_bomb) == "<:red_bomb:1541378662404857906>"
    assert str(skull_bomb) == "<:skull_bomb:1541378690154369124>"


@pytest.mark.parametrize("length", (2, 3, 4))
def test_word_bomb_fragments_always_have_candidates(length: int) -> None:
    fragments = word_bomb_fragments(length)
    assert fragments
    assert all(len(fragment) == length for fragment in fragments)
    assert all(word_bomb_candidates(fragment) for fragment in fragments)


def test_word_bomb_fragment_and_guess_validation() -> None:
    fragment = choose_word_bomb_fragment(2)
    candidates = word_bomb_candidates(fragment)
    assert candidates
    assert is_valid_word_bomb_guess(candidates[0].upper(), fragment.upper())
    assert not is_valid_word_bomb_guess("not-a-word", fragment)
    assert WORD_BOMB_CANDIDATES


def test_word_bomb_uses_extended_words_and_plural_forms() -> None:
    assert "crumb" in WORD_BOMB_WORDS
    assert len(WORD_BOMB_EXTENDED_WORDS) > 300_000
    assert "antidisestablishmentarianism" in WORD_BOMB_EXTENDED_WORDS
    assert is_valid_word_bomb_guess("CrUmB", "MB")
    assert is_valid_word_bomb_guess("crumbs", "mb")


def test_word_bomb_easy_fragments_skip_rare_combinations() -> None:
    fragments = word_bomb_fragments(2, easy=True)

    assert fragments
    assert "pv" not in fragments
    assert "xh" not in fragments


@pytest.mark.asyncio
async def test_word_bomb_lobby_requires_two_players_to_start() -> None:
    game = WordBombGame(
        ctx=cast(Any, SimpleNamespace()),
        channel=cast(Any, SimpleNamespace(id=123)),
        channel_id=123,
        guild_id=None,
        host_id=1,
        players=[WordBombPlayer(1, "host")],
    )
    cog = cast(Any, SimpleNamespace())
    view = WordBombLobbyView(cog, game)
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=response)

    await view._start(cast(Any, interaction))

    response.send_message.assert_awaited_once()
    assert "two players" in response.send_message.await_args.args[0]
    response.defer.assert_not_awaited()
    assert not game.lobby_event.is_set()
