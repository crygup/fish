from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import extensions.fun.minigames as minigames
from extensions.fun.lastletter import is_valid_lastletter_guess


def test_lastletter_accepts_owner_added_wordbomb_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner Word Bomb dictionary is shared with LastLetter validation."""

    monkeypatch.setattr(
        minigames,
        "WORD_BOMB_WORD_LOOKUP",
        set(minigames.WORD_BOMB_WORD_LOOKUP),
    )
    monkeypatch.setattr(minigames, "WORD_BOMB_CUSTOM_WORDS", set())
    monkeypatch.setattr(
        minigames,
        "WORD_BOMB_CANDIDATES",
        {key: tuple(value) for key, value in minigames.WORD_BOMB_CANDIDATES.items()},
    )
    monkeypatch.setattr(
        minigames,
        "WORD_BOMB_FRAGMENT_SETS",
        {key: set(value) for key, value in minigames.WORD_BOMB_FRAGMENT_SETS.items()},
    )
    monkeypatch.setattr(
        minigames,
        "WORD_BOMB_EXTENDED_CANDIDATE_EXAMPLES",
        dict(minigames.WORD_BOMB_EXTENDED_CANDIDATE_EXAMPLES),
    )

    word = "zzcustomword"
    assert minigames.add_word_bomb_words((word,)) == (word,)
    assert minigames.is_valid_word_bomb_guess(word, "zz")
    assert is_valid_lastletter_guess(word, "zz")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name", ("extensions.fun.wordbomb", "extensions.fun.lastletter")
)
async def test_correct_reaction_falls_back_from_answer_to_prompt(
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)
    answer = SimpleNamespace(add_reaction=AsyncMock(side_effect=RuntimeError("denied")))
    prompt = SimpleNamespace(add_reaction=AsyncMock())

    await module.add_correct_answer_reaction(answer, prompt)

    answer.add_reaction.assert_awaited_once_with("✅")
    prompt.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_correct_reaction_ignores_prompt_failure() -> None:
    from extensions.fun.minigames import add_correct_answer_reaction

    answer = SimpleNamespace(add_reaction=AsyncMock(side_effect=RuntimeError("denied")))
    prompt = SimpleNamespace(add_reaction=AsyncMock(side_effect=RuntimeError("gone")))

    await add_correct_answer_reaction(cast(Any, answer), cast(Any, prompt))

    answer.add_reaction.assert_awaited_once_with("✅")
    prompt.add_reaction.assert_awaited_once_with("✅")
