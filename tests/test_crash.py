from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord

from extensions.fun import Fun
from extensions.fun.crash import CRASH_CASHOUT_ALIASES, CrashGame, CrashView


class SafeRng:
    def uniform(self, _low: float, _high: float) -> float:
        return 0.12

    def random(self) -> float:
        return 1.0


class CrashRng(SafeRng):
    def random(self) -> float:
        return 0.0


def test_crash_progression_increases_multiplier_and_payout() -> None:
    game = CrashGame(1, 100, 5, rng=SafeRng())
    assert game.multiplier == 1.0
    assert game.payout == 100
    assert game.advance() is True
    assert game.multiplier == 1.12
    assert game.payout == 112


def test_crash_ends_round_and_loses_reserved_stake() -> None:
    game = CrashGame(1, 100, 5, rng=CrashRng())
    assert game.advance() is False
    assert game.crashed is True
    assert game.finished is True
    assert game.crash_multiplier == 1.0
    assert game.payout == 0


def test_crash_view_is_components_v2_and_cashout_is_enabled() -> None:
    game = CrashGame(1, 100, 5, rng=SafeRng())
    view = CrashView(
        game,
        accent_color=discord.Colour.blurple(),
        on_finish=None,
    )
    assert isinstance(view, discord.ui.LayoutView)
    assert view.cash_out_button.disabled is False
    assert "1.00×" in view.status.content


async def test_crash_message_cashout_uses_the_same_idempotent_finish_path() -> None:
    game = CrashGame(1, 100, 5, rng=SafeRng())
    on_finish = AsyncMock()
    view = CrashView(game, on_finish=on_finish)

    assert await view.cash_out_from_message() is True
    assert game.finished is True
    assert game.cashed_out is True
    assert await view.cash_out_from_message() is False
    on_finish.assert_awaited_once_with(game, "cashout")


def test_crash_message_cashout_aliases() -> None:
    assert CRASH_CASHOUT_ALIASES == {"cashout", "cash out", "co", "cash"}


async def test_crash_message_listener_requires_author_and_game_channel() -> None:
    game = CrashGame(1, 100, 5, rng=SafeRng())
    on_finish = AsyncMock()
    view = CrashView(game, on_finish=on_finish)
    assert game.view is view
    game.message = SimpleNamespace(channel=SimpleNamespace(id=42))
    cog = cast(Any, SimpleNamespace(_crash_games={1: game}))

    await Fun.crash_cashout_listener(
        cog,
        SimpleNamespace(
            author=SimpleNamespace(id=2, bot=False),
            content="cashout",
            channel=SimpleNamespace(id=42),
        ),
    )
    assert game.finished is False

    await Fun.crash_cashout_listener(
        cog,
        SimpleNamespace(
            author=SimpleNamespace(id=1, bot=False),
            content="cash out",
            channel=SimpleNamespace(id=43),
        ),
    )
    assert game.finished is False

    await Fun.crash_cashout_listener(
        cog,
        SimpleNamespace(
            author=SimpleNamespace(id=1, bot=False),
            content="  CASH   OUT ",
            channel=SimpleNamespace(id=42),
        ),
    )
    assert game.cashed_out is True
    on_finish.assert_awaited_once_with(game, "cashout")
