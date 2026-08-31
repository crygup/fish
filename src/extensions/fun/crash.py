"""Crash-style wagering game and its Components V2 view.

The wager is reserved by :class:`core.currency.CurrencyService` before a
``CrashGame`` is created.  The game only keeps the in-progress multiplier in
memory; its final result is persisted through the normal wager settlement
path, so a process restart cannot pay a wager twice.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

if TYPE_CHECKING:
    from extensions.context import Context


CRASH_MIN_BID = 10
# Compatibility export only.  Crash wagers are no longer capped at 10,000;
# validation uses the wallet balance and the shared minimum instead.
CRASH_MAX_BID = 10_000
CRASH_TICK_SECONDS = 1.5
CRASH_GAME_TIMEOUT = 10 * 60
CRASH_CASHOUT_ALIASES = frozenset({"cashout", "cash out", "co", "cash"})
# Keep the opening ticks deliberately modest and let the growth build as the
# round continues.  The final range is reused for every later tick.
CRASH_INCREMENT_RANGES: tuple[tuple[float, float], ...] = (
    (0.04, 0.10),
    (0.05, 0.13),
    (0.07, 0.16),
    (0.09, 0.22),
)

FinishCallback = Callable[["CrashGame", str], Awaitable[None]]


@dataclass(slots=True)
class CrashGame:
    """Mutable state for one crash wager.

    ``advance`` checks for a crash at the currently displayed multiplier
    before increasing it.  This means the opening ``1.00x`` frame can fail on
    the first tick instead of guaranteeing a first multiplier increase.  A
    player can still cash out for their reserved stake before that first tick.
    The failure chance grows gradually with the multiplier rather than relying
    on a fixed probability, which prevents very long, risk-free rounds while
    retaining the requested random progression.
    """

    user_id: int
    stake: int
    wager_id: int
    rng: random.Random | random.SystemRandom = field(
        default_factory=random.SystemRandom, repr=False
    )
    multiplier: float = 1.0
    tick_count: int = 0
    payout: int = 0
    crash_multiplier: float | None = None
    finished: bool = False
    crashed: bool = False
    cashed_out: bool = False
    timed_out: bool = False
    settled: bool = False
    settlement_error: bool = False
    view: CrashView | None = field(default=None, repr=False)
    message: discord.Message | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.stake <= 0:
            raise ValueError("A Crash wager must be positive.")
        self.payout = self.stake

    def _crash_chance(self, multiplier: float) -> float:
        """Return the chance that the round ends at ``multiplier``.

        The chance starts at seven percent and rises with the
        displayed multiplier.  Capping it keeps a single unlucky tick from
        making the board impossible to cash out while ensuring the game does
        not run forever.
        """

        return min(0.07 + max(0.0, multiplier - 1.0) * 0.035, 0.55)

    def _increment_range(self) -> tuple[float, float]:
        """Return the multiplier increment range for the next tick."""

        index = min(self.tick_count, len(CRASH_INCREMENT_RANGES) - 1)
        return CRASH_INCREMENT_RANGES[index]

    def advance(self) -> bool:
        """Advance one tick and return whether the player may continue.

        ``False`` means the currently displayed multiplier crashed before the
        next increase. The random increment is rounded to hundredths so the
        text and payout always use the same value.
        """

        if self.finished:
            return False
        if self.rng.random() < self._crash_chance(self.multiplier):
            self.crash_multiplier = self.multiplier
            self.payout = 0
            self.crashed = True
            self.finished = True
            return False
        increment_min, increment_max = self._increment_range()
        increment = self.rng.uniform(increment_min, increment_max)
        next_multiplier = round(max(self.multiplier + increment, self.multiplier), 2)
        self.multiplier = next_multiplier
        self.tick_count += 1
        self.payout = max(self.stake, int(self.stake * self.multiplier))
        return True

    def cash_out(self) -> None:
        """Cash out the current displayed multiplier exactly once."""

        if self.finished:
            raise ValueError("This Crash game is already over.")
        self.payout = max(self.stake, int(self.stake * self.multiplier))
        self.cashed_out = True
        self.finished = True

    def timeout(self) -> None:
        """Mark an unfinished game as lost when its view expires."""

        if self.finished:
            return
        self.timed_out = True
        self.finished = True
        self.payout = 0


class CrashView(discord.ui.LayoutView):
    """Author-only Components V2 view for a Crash game."""

    def __init__(
        self,
        game: CrashGame,
        *,
        accent_color: discord.Colour | int | None = None,
        on_finish: FinishCallback | None = None,
    ) -> None:
        super().__init__(timeout=CRASH_GAME_TIMEOUT)
        self.game = game
        self.game.view = self
        self.on_finish = on_finish
        self.message: discord.Message | None = None
        self.status = discord.ui.TextDisplay(self._status_text())
        self.container = discord.ui.Container(
            self.status,
            accent_color=accent_color,
        )
        self.add_item(self.container)
        self.cash_out_button = discord.ui.Button(
            label="Cash out",
            style=discord.ButtonStyle.success,
            custom_id=f"crash-cash-out:{game.user_id}:{game.wager_id}",
        )
        self.cash_out_button.callback = self._cash_out
        self.add_item(discord.ui.ActionRow(self.cash_out_button))
        self.refresh()

    def _status_text(self) -> str:
        game = self.game
        if game.settlement_error:
            return "## Crash\nI couldn't settle that wager. Please try again."
        if game.crashed:
            value = game.crash_multiplier or game.multiplier
            return (
                f"## Crash\n💥 Crashed at **{value:.2f}×**. "
                f"You lost **{game.stake:,} Coins**."
            )
        if game.timed_out:
            return "## Crash\nThe game timed out and your wager was lost."
        if game.cashed_out:
            return (
                f"## Crash\nCashed out at **{game.multiplier:.2f}×** for "
                f"**{game.payout:,} Coins**."
            )
        return (
            "## Crash\n"
            f"**{game.multiplier:.2f}×**\n"
            f"-# Wager: **{game.stake:,} Coins** · Cash out: "
            f"**{game.payout:,} Coins**"
        )

    def refresh(self) -> None:
        self.status.content = self._status_text()
        self.cash_out_button.disabled = self.game.finished

    async def _try_cash_out(self) -> bool:
        """Mark the wager as cashed out, returning ``False`` if it is over."""

        async with self.game.lock:
            if self.game.finished:
                return False
            self.game.cash_out()
            self.refresh()
            return True

    async def cash_out_from_message(self) -> bool:
        """Cash out without a component interaction.

        The Fun cog uses this for the author's typed ``cashout`` aliases.  It
        deliberately shares the same lock and finish callback as the button so
        a near-simultaneous button press cannot settle the wager twice.
        """

        if not await self._try_cash_out():
            return False
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, "cashout")
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id and not self.game.finished:
            return True
        message = (
            "This Crash game is over."
            if self.game.finished
            else "Only the person who started this Crash game can play."
        )
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _cash_out(self, interaction: discord.Interaction) -> None:
        if not await self._try_cash_out():
            await interaction.response.send_message(
                "This Crash game is already over.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, "cashout")

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.finished:
                return
            self.game.timeout()
            self.refresh()
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        self.stop()
        if self.on_finish is not None:
            await self.on_finish(self.game, "timeout")


__all__ = (
    "CRASH_CASHOUT_ALIASES",
    "CRASH_GAME_TIMEOUT",
    "CRASH_INCREMENT_RANGES",
    "CRASH_MAX_BID",
    "CRASH_MIN_BID",
    "CRASH_TICK_SECONDS",
    "CrashGame",
    "CrashView",
)
