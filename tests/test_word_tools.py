from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from extensions.fun.minigames import (
    WORD_BOMB_CUSTOM_WORDS,
    WORD_BOMB_WORD_LOOKUP,
    add_word_bomb_words,
    is_valid_word_game_word,
)
from extensions.fun.wordbomb import WordBombCommands


def test_word_game_word_check_uses_the_shared_and_custom_lookup() -> None:
    custom_word = "zzyzx"
    add_word_bomb_words((custom_word,))
    try:
        assert is_valid_word_game_word("CrUmB")
        assert is_valid_word_game_word("crumbs")
        assert is_valid_word_game_word(custom_word.upper())
        assert not is_valid_word_game_word("not-a-word")
    finally:
        WORD_BOMB_CUSTOM_WORDS.discard(custom_word)
        WORD_BOMB_WORD_LOOKUP.discard(custom_word)


def test_checkword_is_hidden_text_only() -> None:
    assert isinstance(WordBombCommands.checkword, commands.Command)
    assert not isinstance(WordBombCommands.checkword, commands.HybridCommand)
    assert WordBombCommands.checkword.hidden


@pytest.mark.asyncio
async def test_checkword_reports_result_without_allowing_mentions() -> None:
    ctx = SimpleNamespace(send=AsyncMock())

    await WordBombCommands.checkword.callback(
        cast(Any, SimpleNamespace()), cast(Any, ctx), word="@everyone"
    )

    message = ctx.send.await_args.args[0]
    assert "not accepted" in message
    assert "@\u200beveryone" in message
    allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
    assert isinstance(allowed_mentions, discord.AllowedMentions)
    assert not allowed_mentions.everyone
    assert not allowed_mentions.users
