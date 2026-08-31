"""Lucky Roll, a small multiplayer dice wagering game.

The lobby and game display intentionally use the same Components V2 layout as
the sea-animal race.  Wagers are reserved from a wallet when a player joins;
they are refunded when a player leaves before the game starts and paid to a
human winner only after a round has produced a single winner.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import discord
from discord.ext import commands

from core.currency import EVERYTHING_AMOUNT, CoinAmountError, parse_coin_amount
from extensions.fun.bot_participants import bot_names, bot_wagers
from utils import get_or_fetch_user

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


LUCKY_ROLL_MAX_PLAYERS = 6
# The host is allowed to play alone; the remaining slots are filled with
# virtual opponents when the lobby starts.
LUCKY_ROLL_MIN_HUMANS = 1
LUCKY_ROLL_LOBBY_TIMEOUT = 60.0
LUCKY_ROLL_UPDATE_SECONDS = 0.5
LUCKY_ROLL_INITIAL_SPIN_SECONDS = 2.0
LUCKY_ROLL_STOP_INTERVAL = 1.5
# Keep the completed rolls visible before striking out the non-contenders.
# This gives users enough time to see what the tied round produced before the
# next animation begins.
LUCKY_ROLL_TIE_DISPLAY_SECONDS = 2.0
LUCKY_ROLL_MIN_BID = 10
# Compatibility export only.  The game now accepts any stake the wallet can
# cover (subject to the shared minimum), so this value is not enforced.
LUCKY_ROLL_MAX_BID = 10_000
LUCKY_ROLL_DEFAULT_BID = 100

_DICE = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣")


def _safe_name(user: object) -> str:
    return str(
        getattr(user, "name", None) or getattr(user, "display_name", None) or "User"
    )


@dataclass(slots=True)
class LuckyRollParticipant:
    user_id: int
    name: str
    wager: int
    is_bot: bool = False
    bot_wager: int = 0
    current_roll: int = 1
    final_roll: int | None = None
    stopped: bool = False
    eliminated: bool = False
    winner: bool = False

    @property
    def display_wager(self) -> int:
        return self.bot_wager if self.is_bot else self.wager


@dataclass(slots=True)
class LuckyRollGame:
    ctx: Context
    host_id: int
    channel_id: int
    guild_id: int | None
    players: list[LuckyRollParticipant] = field(default_factory=list)
    message: discord.Message | None = None
    view: LuckyRollView | None = None
    lobby_task: asyncio.Task[None] | None = field(default=None, repr=False)
    roll_task: asyncio.Task[None] | None = field(default=None, repr=False)
    started: bool = False
    finished: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None
    failed: bool = False
    rolling_again: bool = False
    round_number: int = 0
    pool: int = 0
    winner: LuckyRollParticipant | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _participant_lines(game: LuckyRollGame) -> list[str]:
    lines: list[str] = []
    for participant in game.players:
        name = discord.utils.escape_markdown(participant.name)
        if participant.stopped and participant.final_roll is not None:
            die = _DICE[participant.final_roll - 1]
        else:
            die = _DICE[max(1, min(6, participant.current_roll)) - 1]
        line = f"{name} - {die}"
        lines.append(f"~~{line}~~" if participant.eliminated else line)
    return lines


class LuckyRollView(discord.ui.LayoutView):
    """Lobby/game controls for Lucky Roll."""

    def __init__(self, cog: LuckyRollCommands, game: LuckyRollGame):
        super().__init__(timeout=LUCKY_ROLL_LOBBY_TIMEOUT)
        self.cog = cog
        self.game = game
        self.display = discord.ui.TextDisplay(self._content())
        self.footer = discord.ui.TextDisplay(self._footer())
        self.join = discord.ui.Button(
            label="Join",
            style=discord.ButtonStyle.secondary,
            custom_id=f"luckyroll:join:{game.channel_id}",
        )
        self.leave = discord.ui.Button(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            custom_id=f"luckyroll:leave:{game.channel_id}",
        )
        self.start = discord.ui.Button(
            label="Start",
            style=discord.ButtonStyle.secondary,
            custom_id=f"luckyroll:start:{game.channel_id}",
        )
        self.edit_bid = discord.ui.Button(
            label="Edit Bid",
            style=discord.ButtonStyle.secondary,
            custom_id=f"luckyroll:edit-bid:{game.channel_id}",
        )
        self.join.callback = self._join
        self.leave.callback = self._leave
        self.start.callback = self._start
        self.edit_bid.callback = self._edit_bid
        self.container = discord.ui.Container(
            self.display,
            discord.ui.Separator(),
            self.footer,
            accent_color=getattr(cog.bot, "embedcolor", discord.Colour.blurple()),
        )
        self.add_item(self.container)
        self.add_item(
            discord.ui.ActionRow(self.join, self.leave, self.start, self.edit_bid)
        )
        self.refresh()

    def _content(self) -> str:
        if self.game.cancelled:
            reason = self.game.cancel_reason or "The game was cancelled."
            return f"## Lucky roll\n{reason}"
        if self.game.finished:
            if self.game.winner is None:
                return (
                    "## Lucky roll\n"
                    "The rolls ended without a human winner.\n"
                    f"Total payout: **{self.game.pool:,} Coins**"
                )
            winner = discord.utils.escape_markdown(self.game.winner.name)
            winner_roll = self.game.winner.final_roll or 1
            return (
                "## Lucky roll\n"
                f"{winner} wins with {_DICE[winner_roll - 1]}!\n"
                f"Total payout: **{self.game.pool:,} Coins**"
            )
        title = "## Lucky roll"
        if self.game.rolling_again:
            title += " · Rolling again"
        if not self.game.started:
            players = (
                "\n".join(
                    f"{discord.utils.escape_markdown(p.name)} · {p.wager:,} Coins"
                    for p in self.game.players
                )
                or "No players have joined yet."
            )
            return (
                f"{title}\nJoin the game with the buttons below.\n\n"
                f"Players ({len(self.game.players)}/{LUCKY_ROLL_MAX_PLAYERS})\n{players}"
            )
        return f"{title}\n" + "\n".join(_participant_lines(self.game))

    def _footer(self) -> str:
        humans = " · ".join(
            f"{discord.utils.escape_markdown(p.name)} · {p.wager:,} Coins"
            for p in self.game.players
            if not p.is_bot
        )
        if not humans:
            return f"-# Pool: {self.game.pool:,} Coins"
        return f"-# {humans} · Pool: {self.game.pool:,} Coins"

    def refresh(self) -> None:
        self.display.content = self._content()
        self.footer.content = self._footer()
        lobby = (
            not self.game.started and not self.game.finished and not self.game.cancelled
        )
        self.join.disabled = (
            not lobby or len(self.game.players) >= LUCKY_ROLL_MAX_PLAYERS
        )
        self.leave.disabled = not lobby
        self.start.disabled = not lobby
        self.edit_bid.disabled = not lobby

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id != self.game.channel_id:
            await interaction.response.send_message(
                "This game is in another channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def _join(self, interaction: discord.Interaction) -> None:
        await self.cog._join_luckyroll(interaction, self.game)

    async def _leave(self, interaction: discord.Interaction) -> None:
        await self.cog._leave_luckyroll(interaction, self.game)

    async def _start(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message(
                "Only the person who started the game can start it early.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.cog._begin_luckyroll(self.game, interaction=interaction)

    async def _edit_bid(self, interaction: discord.Interaction) -> None:
        if self.game.started or self.game.finished or self.game.cancelled:
            await interaction.response.send_message(
                "The Lucky Roll lobby is closed.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        participant = self.cog._luckyroll_participant_for(
            self.game, interaction.user.id
        )
        if participant is None:
            await interaction.response.send_message(
                "Join Lucky Roll before editing your bid.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(
            LuckyRollBidModal(self.cog, self.game, participant.wager)
        )

    async def on_timeout(self) -> None:
        if not self.game.started and not self.game.finished:
            await self.cog._begin_luckyroll(self.game)


class LuckyRollBidModal(discord.ui.Modal, title="Edit Lucky Roll bid"):
    bid = discord.ui.TextInput(
        label="Bid amount",
        placeholder="10+ Coins",
        min_length=2,
        max_length=20,
        required=True,
    )

    def __init__(self, cog: "LuckyRollCommands", game: LuckyRollGame, current_bid: int):
        super().__init__()
        self.cog = cog
        self.game = game
        self.bid.default = str(current_bid)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            parsed = parse_coin_amount(str(self.bid.value))
            if parsed == EVERYTHING_AMOUNT:
                raise CoinAmountError(
                    "Use a numeric bid here; use the command with `everything` "
                    "to confirm bidding your full wallet."
                )
            amount = int(parsed)
        except CoinAmountError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if amount < LUCKY_ROLL_MIN_BID:
            await interaction.response.send_message(
                "Bids must be at least 10 Coins.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.cog._edit_luckyroll_bid(interaction, self.game, amount)


class LuckyRollCommands:
    """Text Lucky Roll command and statistics helpers."""

    bot: Fishie
    _luckyroll_games: dict[int, LuckyRollGame]

    @cast(Any, commands.group)(
        name="luckyroll", aliases=("lucky-roll", "lr"), invoke_without_command=True
    )
    async def luckyroll(self, ctx: Context, *, amount: str | None = None) -> None:
        """Roll dice against other players for Coins."""

        await self._start_luckyroll(ctx, amount)

    @luckyroll.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def luckyroll_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Lucky Roll wins, losses, earnings, and losses."""

        await self._send_luckyroll_stats(ctx, user)

    def _luckyroll_participant_for(
        self, game: LuckyRollGame, user_id: int
    ) -> LuckyRollParticipant | None:
        return next(
            (p for p in game.players if p.user_id == user_id and not p.is_bot), None
        )

    async def _reserve_luckyroll_wager(self, user_id: int, amount: int) -> bool:
        try:
            await self.bot.currency.debit(user_id, amount, "luckyroll_wager")
        except Exception:
            return False
        return True

    async def _refund_luckyroll_wager(self, user_id: int, amount: int) -> bool:
        try:
            await self.bot.currency.credit(user_id, amount, "luckyroll_refund")
            return True
        except Exception:
            self.bot.logger.exception("Failed to refund Lucky Roll wager")
            return False

    async def _start_luckyroll(self, ctx: Context, amount: object | None) -> None:
        if not await cast(Any, self)._require_currency_tracking(ctx):
            return
        channel_id = int(ctx.channel.id)
        current = self._luckyroll_games.get(channel_id)
        if current is not None and not current.finished and not current.cancelled:
            await ctx.send(
                "There is already a Lucky Roll game in this channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        resolver = getattr(self, "_resolve_coin_amount", None)
        if callable(resolver):
            bid, stop = await cast(Any, resolver)(
                ctx,
                amount,
                default=LUCKY_ROLL_DEFAULT_BID,
                minimum=LUCKY_ROLL_MIN_BID,
            )
            if stop or bid is None:
                return
        else:
            try:
                from core.currency import CoinAmountError, parse_coin_amount

                parsed = parse_coin_amount(
                    LUCKY_ROLL_DEFAULT_BID if amount is None else amount
                )
                if isinstance(parsed, str):
                    raise CoinAmountError("A wallet is required for everything.")
                bid = int(parsed)
            except (CoinAmountError, TypeError, ValueError):
                await ctx.send(
                    "Enter a valid Coin wager.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        if bid < LUCKY_ROLL_MIN_BID:
            await ctx.send(
                f"Bids must be at least {LUCKY_ROLL_MIN_BID:,} Coins.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._reserve_luckyroll_wager(ctx.author.id, bid):
            await ctx.send(
                "You do not have enough Coins for that bid.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        participant = LuckyRollParticipant(ctx.author.id, _safe_name(ctx.author), bid)
        game = LuckyRollGame(
            ctx,
            ctx.author.id,
            channel_id,
            ctx.guild.id if ctx.guild else None,
            players=[participant],
        )
        self._luckyroll_games[channel_id] = game
        view = LuckyRollView(self, game)
        game.view = view
        try:
            game.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            self._luckyroll_games.pop(channel_id, None)
            await self._refund_luckyroll_wager(ctx.author.id, bid)
            raise
        game.lobby_task = asyncio.create_task(self._luckyroll_lobby_timer(game))

    async def _luckyroll_lobby_timer(self, game: LuckyRollGame) -> None:
        try:
            await asyncio.sleep(LUCKY_ROLL_LOBBY_TIMEOUT)
            await self._begin_luckyroll(game)
        except asyncio.CancelledError:
            return
        except Exception:
            self.bot.logger.exception("Failed to start Lucky Roll lobby")

    async def _join_luckyroll(
        self, interaction: discord.Interaction, game: LuckyRollGame
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                await interaction.response.send_message(
                    "This Lucky Roll game has already started.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self._luckyroll_participant_for(game, interaction.user.id) is not None:
                await interaction.response.send_message(
                    "You already joined this game.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if len(game.players) >= LUCKY_ROLL_MAX_PLAYERS:
                await interaction.response.send_message(
                    "This Lucky Roll game is full.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not await self._reserve_luckyroll_wager(
                interaction.user.id, LUCKY_ROLL_DEFAULT_BID
            ):
                await interaction.response.send_message(
                    "You do not have enough Coins to join.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            game.players.append(
                LuckyRollParticipant(
                    interaction.user.id,
                    _safe_name(interaction.user),
                    LUCKY_ROLL_DEFAULT_BID,
                )
            )
            if game.view is not None:
                game.view.refresh()
            await interaction.response.edit_message(
                view=game.view, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _edit_luckyroll_bid(
        self, interaction: discord.Interaction, game: LuckyRollGame, amount: int
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                await interaction.response.send_message(
                    "The Lucky Roll lobby is closed.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            participant = self._luckyroll_participant_for(game, interaction.user.id)
            if participant is None:
                await interaction.response.send_message(
                    "Join Lucky Roll before editing your bid.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            difference = amount - participant.wager
            if difference > 0 and not await self._reserve_luckyroll_wager(
                participant.user_id, difference
            ):
                await interaction.response.send_message(
                    "You do not have enough Coins for that bid.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if difference < 0 and not await self._refund_luckyroll_wager(
                participant.user_id, -difference
            ):
                await interaction.response.send_message(
                    "I couldn't update your bid right now. Please try again.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            participant.wager = amount
            if game.view is not None:
                game.view.refresh()
            await interaction.response.edit_message(
                view=game.view, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _leave_luckyroll(
        self, interaction: discord.Interaction, game: LuckyRollGame
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                await interaction.response.send_message(
                    "The Lucky Roll lobby is no longer open.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            participant = self._luckyroll_participant_for(game, interaction.user.id)
            if participant is None:
                await interaction.response.send_message(
                    "You are not in this game.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            game.players.remove(participant)
            await self._refund_luckyroll_wager(participant.user_id, participant.wager)
            if not game.players:
                game.cancelled = True
                self._luckyroll_games.pop(game.channel_id, None)
                if game.lobby_task is not None:
                    game.lobby_task.cancel()
            if game.view is not None:
                game.view.refresh()
            await interaction.response.edit_message(
                view=game.view, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _begin_luckyroll(
        self, game: LuckyRollGame, *, interaction: discord.Interaction | None = None
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                if interaction is not None and not interaction.response.is_done():
                    await interaction.response.send_message(
                        "This Lucky Roll game has already started.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return
            human_count = sum(not p.is_bot for p in game.players)
            if human_count < LUCKY_ROLL_MIN_HUMANS:
                game.cancelled = True
                game.cancel_reason = "Lucky Roll needs at least 1 player."
                for participant in game.players:
                    if not participant.is_bot:
                        await self._refund_luckyroll_wager(
                            participant.user_id, participant.wager
                        )
                self._luckyroll_games.pop(game.channel_id, None)
                if game.lobby_task is not None:
                    game.lobby_task.cancel()
                if game.view is not None:
                    game.view.refresh()
                if interaction is not None and not interaction.response.is_done():
                    await interaction.response.edit_message(
                        view=game.view, allowed_mentions=discord.AllowedMentions.none()
                    )
                elif game.message is not None and game.view is not None:
                    await game.message.edit(
                        view=game.view, allowed_mentions=discord.AllowedMentions.none()
                    )
                return
            if (
                game.lobby_task is not None
                and game.lobby_task is not asyncio.current_task()
            ):
                game.lobby_task.cancel()
            bot_count = LUCKY_ROLL_MAX_PLAYERS - len(game.players)
            human_bids = [
                participant.wager
                for participant in game.players
                if not participant.is_bot
            ]
            wagers = bot_wagers(
                human_bids,
                bot_count,
                minimum=LUCKY_ROLL_MIN_BID,
                fallback_maximum=100,
            )
            names = bot_names(
                bot_count,
                guild=game.ctx.guild,
                reserved=(participant.name for participant in game.players),
            )
            for name, wager in zip(names, wagers):
                number = len(game.players) + 1
                game.players.append(
                    LuckyRollParticipant(
                        user_id=-number,
                        name=name,
                        wager=0,
                        is_bot=True,
                        bot_wager=wager,
                    )
                )
            game.pool = sum(p.display_wager for p in game.players)
            game.started = True
            if game.view is not None:
                game.view.refresh()
            if interaction is not None:
                if interaction.response.is_done():
                    await interaction.edit_original_response(
                        view=game.view, allowed_mentions=discord.AllowedMentions.none()
                    )
                else:
                    await interaction.response.edit_message(
                        view=game.view, allowed_mentions=discord.AllowedMentions.none()
                    )
            elif game.message is not None and game.view is not None:
                await game.message.edit(
                    view=game.view, allowed_mentions=discord.AllowedMentions.none()
                )
            game.roll_task = asyncio.create_task(self._luckyroll_loop(game))

    async def _edit_game(self, game: LuckyRollGame) -> None:
        if game.view is not None:
            game.view.refresh()
        if game.message is not None and game.view is not None:
            try:
                await game.message.edit(
                    view=game.view, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass

    async def _roll_round(
        self, game: LuckyRollGame, contenders: list[LuckyRollParticipant]
    ) -> None:
        for participant in contenders:
            participant.stopped = False
            participant.final_roll = None
        start = monotonic()
        stop_at = {
            id(participant): start
            + LUCKY_ROLL_INITIAL_SPIN_SECONDS
            + index * LUCKY_ROLL_STOP_INTERVAL
            for index, participant in enumerate(contenders)
        }
        while True:
            now = monotonic()
            all_stopped = True
            for participant in contenders:
                if participant.stopped:
                    continue
                all_stopped = False
                if now >= stop_at[id(participant)]:
                    participant.final_roll = random.randint(1, 6)
                    participant.current_roll = participant.final_roll
                    participant.stopped = True
                else:
                    participant.current_roll = random.randint(1, 6)
            await self._edit_game(game)
            if all_stopped or all(p.stopped for p in contenders):
                break
            await asyncio.sleep(LUCKY_ROLL_UPDATE_SECONDS)

    async def _luckyroll_loop(self, game: LuckyRollGame) -> None:
        try:
            while not game.finished and not game.cancelled:
                game.round_number += 1
                game.rolling_again = game.round_number > 1
                contenders = [p for p in game.players if not p.eliminated]
                if not contenders:
                    game.failed = True
                    game.finished = True
                    break
                await self._roll_round(game, contenders)
                highest = max(int(p.final_roll or 1) for p in contenders)
                tied = [p for p in contenders if p.final_roll == highest]
                if len(tied) == 1:
                    winner = tied[0]
                    winner.winner = True
                    game.winner = winner if not winner.is_bot else None
                    game.finished = True
                    await self._settle_luckyroll(game)
                    await self._edit_game(game)
                    break
                await asyncio.sleep(LUCKY_ROLL_TIE_DISPLAY_SECONDS)
                tied_ids = {id(p) for p in tied}
                for participant in contenders:
                    if id(participant) not in tied_ids:
                        participant.eliminated = True
                # A tie consisting only of bots cannot produce a human winner;
                # do not leave a payout stranded in that case.
                if not any(not p.is_bot for p in tied):
                    game.failed = True
                    game.finished = True
                    await self._settle_luckyroll(game)
                    await self._edit_game(game)
                    break
                game.rolling_again = True
                await self._edit_game(game)
        except asyncio.CancelledError:
            return
        except Exception:
            self.bot.logger.exception("Lucky Roll game loop failed")

    async def _settle_luckyroll(self, game: LuckyRollGame) -> None:
        for participant in game.players:
            if participant.is_bot:
                continue
            won = game.winner is participant
            if won:
                await self._credit_luckyroll(participant.user_id, game.pool)
            await self._record_luckyroll_stat(
                participant, won=won, earnings=game.pool if won else 0
            )
        if game.view is not None:
            game.view.join.disabled = True
            game.view.leave.disabled = True
            game.view.start.disabled = True
            game.view.edit_bid.disabled = True

    async def _credit_luckyroll(self, user_id: int, amount: int) -> None:
        try:
            await self.bot.currency.credit(user_id, amount, "luckyroll_win")
        except Exception:
            self.bot.logger.exception("Failed to award Lucky Roll payout")

    async def _record_luckyroll_stat(
        self, participant: LuckyRollParticipant, *, won: bool, earnings: int
    ) -> None:
        try:
            await self.bot.pool.execute(
                """
                INSERT INTO lucky_roll_stats
                    (user_id, wins, losses, coins_earned, coins_lost)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (user_id) DO UPDATE SET
                    wins = lucky_roll_stats.wins + EXCLUDED.wins,
                    losses = lucky_roll_stats.losses + EXCLUDED.losses,
                    coins_earned = lucky_roll_stats.coins_earned + EXCLUDED.coins_earned,
                    coins_lost = lucky_roll_stats.coins_lost + EXCLUDED.coins_lost,
                    updated_at = now()
                """,
                participant.user_id,
                int(won),
                int(not won),
                earnings,
                0 if won else participant.wager,
            )
        except Exception:
            self.bot.logger.exception("Failed to record Lucky Roll statistics")

    async def _send_luckyroll_stats(
        self, ctx: Context, user: discord.User | None = None
    ) -> None:
        target = user or ctx.author
        visible = (
            target.id == ctx.author.id
            or self.bot.db_cache.game_history_visible_to(target.id, ctx.author.id)
        )
        selected = (
            await self.bot.pool.fetchrow(
                "SELECT wins, losses, coins_earned, coins_lost "
                "FROM lucky_roll_stats WHERE user_id = $1",
                target.id,
            )
            if visible
            else None
        )
        rows = await self.bot.pool.fetch(
            "SELECT user_id, wins, losses, coins_earned, coins_lost "
            "FROM lucky_roll_stats ORDER BY wins DESC, coins_earned DESC, user_id"
        )
        rows = [
            row
            for row in rows
            if self.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ][:5]
        safe = discord.utils.escape_markdown(_safe_name(target))
        values = selected or {
            "wins": 0,
            "losses": 0,
            "coins_earned": 0,
            "coins_lost": 0,
        }
        lines = [
            f"## Lucky Roll stats for {safe}",
            f"**Wins:** {int(values['wins']):,} · **Losses:** {int(values['losses']):,}",
            f"**Earnings:** {int(values['coins_earned']):,} Coins · **Lost:** {int(values['coins_lost']):,} Coins",
            "### Most wins",
        ]
        if rows:
            for index, row in enumerate(rows, 1):
                user_obj = self.bot.get_user(int(row["user_id"]))
                if user_obj is None:
                    try:
                        user_obj = await get_or_fetch_user(
                            self.bot, int(row["user_id"])
                        )
                    except (discord.HTTPException, discord.NotFound):
                        user_obj = None
                name = _safe_name(user_obj) if user_obj else str(row["user_id"])
                lines.append(
                    f"**#{index} {discord.utils.escape_markdown(name)}** · "
                    f"{int(row['wins']):,} wins"
                )
        else:
            lines.append("No Lucky Roll games have been recorded yet.")
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=getattr(self.bot, "embedcolor", discord.Colour.blurple()),
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())
