"""Weighted three-reel Slots game and its Components V2 display."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


SLOTS_MIN_BID = 10
# Compatibility export only.  Slots wagers are no longer capped at 10,000;
# validation uses the wallet balance and the shared minimum instead.
SLOTS_MAX_BID = 10_000
SLOTS_MAX_ROUNDS = 10
SLOTS_DEFAULT_BID = 100
SLOTS_CURSOR_DELAY = 1.0
SLOTS_REVEAL_INTERVAL = 1.5
SLOTS_SOURCE = "slots"

# The supplied percentages sum to 100.5% because the source table rounds each
# value independently.  random.choices normalises these weights, preserving
# the intended relative odds while keeping every symbol selectable.
SLOT_SYMBOLS: tuple[str, ...] = (
    "🍒",
    "🍋",
    "🍇",
    "🔔",
    "💎",
    "7️⃣",
    "🌽",
)
SLOT_WEIGHTS: tuple[float, ...] = (35.0, 25.0, 18.0, 12.0, 7.0, 3.0, 0.5)
SLOT_PAYOUTS: dict[str, tuple[float, float]] = {
    "🍒": (0.90, 3.0),
    "🍋": (1.10, 6.0),
    "🍇": (1.25, 12.0),
    "🔔": (2.0, 25.0),
    "💎": (4.0, 75.0),
    "7️⃣": (8.0, 300.0),
    "🌽": (20.0, 670.0),
}


def _payout_for(reels: tuple[str, ...], stake: int) -> tuple[int, float, str | None]:
    """Return payout, multiplier and matching symbol for a completed spin."""

    if len(reels) != 3:
        raise ValueError("Slots requires exactly three reels.")
    if reels[0] == reels[1] == reels[2]:
        multiplier = SLOT_PAYOUTS[reels[0]][1]
        return int(stake * multiplier), multiplier, reels[0]
    if reels[0] == reels[1]:
        symbol = reels[0]
    elif reels[0] == reels[2]:
        symbol = reels[0]
    elif reels[1] == reels[2]:
        symbol = reels[1]
    else:
        return 0, 0.0, None
    multiplier = SLOT_PAYOUTS[symbol][0]
    return int(stake * multiplier), multiplier, symbol


def choose_reels(rng: random.Random | Any = random) -> tuple[str, str, str]:
    """Choose one weighted symbol for each reel."""

    return cast(
        tuple[str, str, str],
        tuple(rng.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)),
    )


@dataclass(slots=True)
class SlotsGame:
    ctx: Context
    user_id: int
    stake: int
    wager_id: int
    reels: tuple[str, str, str] = field(default_factory=lambda: ("🟦", "🟦", "🟦"))
    revealed: list[str | None] = field(default_factory=lambda: [None, None, None])
    cursor: int = 0
    cursor_visible: bool = False
    payout: int = 0
    multiplier: float = 0.0
    matching_symbol: str | None = None
    finished: bool = False
    message: discord.Message | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    settled: bool = False


@dataclass(slots=True)
class SlotsSession:
    """A group of spins rendered and animated in one message."""

    ctx: Context
    user_id: int
    games: list[SlotsGame]
    message: discord.Message | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    finished: bool = False

    @property
    def stake(self) -> int:
        return sum(game.stake for game in self.games)

    @property
    def payout(self) -> int:
        return sum(game.payout for game in self.games)


def slot_line(game: SlotsGame) -> str:
    """Render the fish cursor moving across the three unrevealed reels."""

    visible = [symbol or "🟦" for symbol in game.revealed]
    if game.finished:
        return "".join(visible)
    # Keep the opening board clean for a moment.  The fish cursor enters on
    # the first update, rather than appearing in front of the reels in the
    # initial message.
    if not game.cursor_visible:
        return "".join(visible)
    cursor = max(0, min(3, game.cursor))
    if cursor >= 3:
        return "".join(visible)
    # The fish occupies the current reel; it is a cursor, not a fourth reel.
    visible[cursor] = "🐟"
    return "".join(visible)


class SlotsView(discord.ui.LayoutView):
    """Components V2 display for a Slots spin."""

    def __init__(self, cog: SlotsCommands, game: SlotsGame | SlotsSession):
        super().__init__(timeout=None)
        self.cog = cog
        self.game = game
        self.games = game.games if isinstance(game, SlotsSession) else [game]
        self.display = discord.ui.TextDisplay(self._content())
        self.details = discord.ui.TextDisplay(self._details())
        self.summary = discord.ui.TextDisplay(self._summary())
        container_items: list[discord.ui.Item[Any]] = [self.display]
        if len(self.games) == 1:
            container_items.extend((discord.ui.Separator(), self.details))
        else:
            container_items.extend((discord.ui.Separator(), self.summary))
        self.container = discord.ui.Container(
            *container_items,
            accent_color=getattr(cog.bot, "embedcolor", discord.Colour.blurple()),
        )
        self.add_item(self.container)

    @staticmethod
    def _result_text(game: SlotsGame) -> str:
        if not game.payout:
            return "No match · 0 Coins"
        symbol = game.matching_symbol or ""
        match = "3-match" if game.reels.count(symbol) == 3 else f"2-match {symbol}"
        return f"{match} · {game.payout:,} Coins"

    def _content(self) -> str:
        if len(self.games) == 1:
            return f"## Slots\n{slot_line(self.games[0])}"
        rows = [
            (
                f"{slot_line(game)} · {self._result_text(game)}"
                if game.finished
                else slot_line(game)
            )
            for game in self.games
        ]
        return "## Slots\n" + "\n".join(rows)

    def _details(self) -> str:
        if not all(game.finished for game in self.games):
            wager = sum(game.stake for game in self.games)
            suffix = f" · **{len(self.games)} spins**" if len(self.games) > 1 else ""
            return f"-# Wager: **{wager:,} Coins**{suffix}"
        if len(self.games) == 1:
            game = self.games[0]
            if game.payout:
                symbol = game.matching_symbol or ""
                match = "3-match" if game.reels.count(symbol) == 3 else "2-match"
                return (
                    f"-# {match} {symbol} · **{game.multiplier:g}×** · "
                    f"Payout: **{game.payout:,} Coins**"
                )
            return "-# No match · **0 Coins**"

        results: list[str] = []
        for index, game in enumerate(self.games, start=1):
            if game.payout:
                symbol = game.matching_symbol or ""
                match = "3-match" if game.reels.count(symbol) == 3 else "2-match"
                results.append(
                    f"**{index}.** {match} {symbol} · **{game.payout:,} Coins**"
                )
            else:
                results.append(f"**{index}.** No match · **0 Coins**")
        return (
            f"-# Total wager: **{sum(game.stake for game in self.games):,} Coins** · "
            f"Total payout: **{sum(game.payout for game in self.games):,} Coins**\n"
            + "\n".join(results)
        )

    def _summary(self) -> str:
        """Render the compact total shown below a multi-spin grid."""

        return (
            f"-# Total spent: **{sum(game.stake for game in self.games):,} Coins** · "
            f"Total payout: **{sum(game.payout for game in self.games):,} Coins**"
        )

    def refresh(self) -> None:
        self.display.content = self._content()
        self.details.content = self._details()
        self.summary.content = self._summary()


class SlotsCommands:
    """Text Slots command and the shared game implementation."""

    bot: Fishie
    _slots_games: dict[int, SlotsSession]

    @cast(Any, commands.command)(
        name="slots",
        aliases=("slot",),
        extras={"usage": "[amount] [count]"},
    )
    async def slots(self, ctx: Context, *arguments: str) -> None:
        """Spin one or more weighted three-reel slots for at least 10 Coins."""

        amount: str | None = None
        count = 1
        tokens = [token for token in arguments if str(token).strip()]
        # ``slots <wager> <count>`` remains compatible with the original
        # command while allowing a spaced wager expression in the first
        # position (for example ``slots 1 hundred thousand 5``).
        if tokens:
            if len(tokens) > 1:
                try:
                    candidate = int(tokens[-1].replace(",", ""))
                except (TypeError, ValueError):
                    candidate = None
                if candidate is not None and 1 <= candidate <= SLOTS_MAX_ROUNDS:
                    count = candidate
                    tokens.pop()
            amount = " ".join(tokens)
        await self._start_slots(ctx, amount, count)

    async def _start_slots(
        self, ctx: Context, amount: object | None = None, count: int = 1
    ) -> None:
        checker = getattr(self, "_require_currency_tracking", None)
        if callable(checker) and not await cast(Any, self)._require_currency_tracking(
            ctx
        ):
            return
        user_id = int(ctx.author.id)
        active = self._slots_games.get(user_id)
        if active is not None and not active.finished:
            await ctx.send(
                "You already have an active Slots game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not 1 <= count <= SLOTS_MAX_ROUNDS:
            await ctx.send(
                f"You can spin between 1 and {SLOTS_MAX_ROUNDS} slots at once.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        stake, stop = await self._resolve_coin_amount(
            ctx, amount, default=SLOTS_DEFAULT_BID, minimum=SLOTS_MIN_BID
        )
        if stop or stake is None:
            return
        if stake < SLOTS_MIN_BID:
            await ctx.send(
                f"Bids must be at least {SLOTS_MIN_BID:,} Coins.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        games: list[SlotsGame] = []
        try:
            for _ in range(count):
                wager = await self.bot.currency.open_wager(
                    user_id,
                    stake,
                    source=SLOTS_SOURCE,
                )
                games.append(
                    SlotsGame(ctx, user_id, stake, wager.id, reels=choose_reels())
                )
        except Exception as exc:
            from core.currency import InsufficientFunds, InvalidAmount

            # Every wager is reserved independently.  If a multi-spin request
            # cannot reserve all of its stakes, return the already reserved
            # stakes before reporting the error.
            for game in games:
                try:
                    await self.bot.currency.settle_wager(
                        game.wager_id, game.stake, track_stats=False
                    )
                except Exception:
                    self.bot.logger.exception("Failed to refund partial Slots wager")

            if isinstance(exc, InsufficientFunds):
                message = "You do not have enough Coins for that bid."
            elif isinstance(exc, InvalidAmount):
                # Keep the user-facing validation in sync with the Slots
                # command even if a stale service or database constraint
                # rejects the wager.
                message = f"Bids must be at least {SLOTS_MIN_BID:,} Coins."
            else:
                self.bot.logger.exception("Failed to open Slots wager")
                message = "I couldn't start that Slots game. Please try again."
            await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
            return

        session = SlotsSession(ctx, user_id, games)
        self._slots_games[user_id] = session
        view = SlotsView(self, session)
        try:
            session.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            session.task = asyncio.create_task(self._run_slots(session, view))
        except Exception:
            self._slots_games.pop(user_id, None)
            for game in games:
                try:
                    await self.bot.currency.settle_wager(
                        game.wager_id, game.stake, track_stats=False
                    )
                except Exception:
                    self.bot.logger.exception("Failed to refund Slots wager after send")
            raise

    async def _run_slots(
        self, session: SlotsSession | SlotsGame, view: SlotsView
    ) -> None:
        # Keep the worker callable with a single SlotsGame for extensions or
        # tests that used the original one-spin implementation directly.
        if isinstance(session, SlotsGame):
            session = SlotsSession(
                session.ctx,
                session.user_id,
                [session],
                message=session.message,
                task=session.task,
            )
        try:
            await asyncio.sleep(SLOTS_CURSOR_DELAY)
            for game in session.games:
                game.cursor_visible = True
            view.refresh()
            if session.message is not None:
                await session.message.edit(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            for index in range(3):
                await asyncio.sleep(SLOTS_REVEAL_INTERVAL)
                for game in session.games:
                    game.revealed[index] = game.reels[index]
                    game.cursor = index + 1
                view.refresh()
                if session.message is not None:
                    await session.message.edit(
                        view=view, allowed_mentions=discord.AllowedMentions.none()
                    )
            for game in session.games:
                game.payout, game.multiplier, game.matching_symbol = _payout_for(
                    game.reels, game.stake
                )
                game.finished = True
                await self.bot.currency.settle_wager(game.wager_id, game.payout)
                game.settled = True
            session.finished = True
            view.refresh()
            if session.message is not None:
                await session.message.edit(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
        except asyncio.CancelledError:
            # A cog reload or shutdown must not strand the reserved stake.
            for game in session.games:
                if game.settled:
                    continue
                try:
                    await self.bot.currency.settle_wager(
                        game.wager_id, game.stake, track_stats=False
                    )
                except Exception:
                    self.bot.logger.exception("Failed to refund cancelled Slots wager")
            return
        except Exception:
            self.bot.logger.exception("Slots game loop failed")
            for game in session.games:
                if game.settled:
                    continue
                try:
                    await self.bot.currency.settle_wager(
                        game.wager_id, game.stake, track_stats=False
                    )
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund Slots wager after failure"
                    )
                game.finished = True
            session.finished = True
            view.refresh()
            if session.message is not None:
                try:
                    await session.message.edit(
                        view=view, allowed_mentions=discord.AllowedMentions.none()
                    )
                except discord.HTTPException:
                    pass
        finally:
            self._slots_games.pop(session.user_id, None)
