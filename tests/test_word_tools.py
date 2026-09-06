from __future__ import annotations

import sys
from pathlib import Path
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
    short_word_game_words,
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


def test_short_word_export_is_unique_and_deterministic() -> None:
    assert short_word_game_words(("BEE", "aa", "bee", "AA", "a", "no!")) == (
        "aa",
        "bee",
    )


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


def test_export_short_words_writes_utf8_lines(tmp_path: Any) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from scripts.export_word_lists import export_short_words

    output = tmp_path / "2-3letters.txt"

    count = export_short_words(output)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert count == len(lines)
    assert lines == sorted(set(lines))
    assert all(word.isalpha() and len(word) in (2, 3) for word in lines)
    assert "abc" in lines
