"""Sea Animal Race lobby, game loop, and Components V2 interface."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import discord
from discord.ext import commands

from core.currency import EVERYTHING_AMOUNT, CoinAmountError, parse_coin_amount
from extensions.fun.bot_participants import bot_names, bot_wagers
from utils import get_or_fetch_user
from utils.emojis import race_animals, race_blue_square

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


RACE_ROWS = 5
RACE_COLUMNS = 6
RACE_MAX_PLAYERS = 5
RACE_LOBBY_TIMEOUT = 60.0
RACE_TICK_SECONDS = 1.5
RACE_MIN_BID = 10
# Compatibility export only.  Race wagers are no longer capped at 10,000;
# validation uses the wallet balance and the shared minimum instead.
RACE_MAX_BID = 10_000
RACE_DEFAULT_BID = 100
RACE_ANIMAL_COUNT = len(race_animals)


@dataclass(slots=True)
class RaceParticipant:
    user_id: int
    name: str
    animal: str
    wager: int
    is_bot: bool = False
    bot_wager: int = 0
    position: int = 0
    alive: bool = True
    winner: bool = False
    fainted: bool = False
    # Racing emoji ownership is resolved when a participant joins and again
    # when bots are filled into the lobby.  Keep the resolved value on the
    # participant so refreshing the lobby never performs a database lookup.
    preferred_emoji: str | None = None
    preferred_emoji_key: tuple[str, int | str] | None = None
    preferred_is_sea_animal: bool = False
    preferred_emoji_balance: int = 0
    # Every purchased sea-animal emoji is reserved for its owner.  The
    # equipped emoji is the one rendered for that participant, but an owned
    # (not currently equipped) default animal must not be handed to another
    # lane or to a Fishie bot either.
    reserved_sea_animal_keys: set[tuple[str, int | str]] = field(default_factory=set)
    racing_emoji_loaded: bool = False

    @property
    def display_animal(self) -> str:
        return "\u274c" if self.fainted else self.animal


@dataclass(slots=True)
class RaceGame:
    ctx: Context
    host_id: int
    channel_id: int
    guild_id: int | None
    players: list[RaceParticipant] = field(default_factory=list)
    message: discord.Message | None = None
    view: SeaAnimalRaceView | None = None
    lobby_task: asyncio.Task[None] | None = field(default=None, repr=False)
    race_task: asyncio.Task[None] | None = field(default=None, repr=False)
    started: bool = False
    finished: bool = False
    cancelled: bool = False
    winner: RaceParticipant | None = None
    pool: int = 0
    tick_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _safe_name(user: object) -> str:
    return str(
        getattr(user, "name", None) or getattr(user, "display_name", None) or "User"
    )


def _row_value(row: object, key: str, default: Any = None) -> Any:
    """Read a value from an asyncpg record or a normal mapping safely."""

    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _racing_emoji_from_row(
    row: object,
) -> tuple[str, tuple[str, int | str], bool] | None:
    """Return a safe Discord display string and stable ownership key.

    The catalog validates these fields when an emoji is purchased.  This
    second, deliberately defensive check keeps a stale or hand-edited row
    from breaking a race if the emoji is no longer available.
    """

    name = str(_row_value(row, "emoji_name", "") or "").strip()
    if not name:
        return None
    raw_id = _row_value(row, "emoji_id")
    if raw_id is None:
        # Unicode emoji keys use the exact rendered character.  The race
        # catalog only accepts one emoji per purchase, so no further parsing
        # is needed here.
        category = (
            str(_row_value(row, "category", "") or "").casefold().replace("-", "_")
        )
        is_sea_animal = name in race_animals or category in {"sea", "sea_animal"}
        return name, ("unicode", name), is_sea_animal
    try:
        emoji_id = int(raw_id)
    except (TypeError, ValueError, OverflowError):
        return None
    if emoji_id <= 0 or len(name) > 32:
        return None
    animated = bool(_row_value(row, "animated", False))
    prefix = "a" if animated else ""
    category = str(_row_value(row, "category", "") or "").casefold().replace("-", "_")
    is_sea_animal = category in {"sea", "sea_animal"}
    return f"<{prefix}:{name}:{emoji_id}>", ("custom", emoji_id), is_sea_animal


def race_grid(game: RaceGame) -> str:
    """Render the five lanes and six progress columns."""

    lines: list[str] = []
    for participant in game.players[:RACE_ROWS]:
        cells = [race_blue_square] * RACE_COLUMNS
        index = min(max(participant.position, 0), RACE_COLUMNS - 1)
        cells[index] = participant.display_animal
        lines.append("".join(cells))
    while len(lines) < RACE_ROWS:
        lines.append(race_blue_square * RACE_COLUMNS)
    return "\n".join(lines)


def movement_success(
    *, solo_human: bool, is_bot: bool, rng: random.Random | Any
) -> bool:
    """Return whether a racer advances this tick.

    A lone human receives the requested 55/45 advantage.  All other racers use
    an even split.
    """

    if solo_human and not is_bot:
        return rng.random() < 0.55
    return rng.random() < 0.50


def injury_roll(*, solo_human: bool, is_bot: bool, rng: random.Random | Any) -> bool:
    """Return whether a racer faints this tick."""

    chance = 0.04
    if solo_human and not is_bot:
        chance = 0.05
    return rng.random() < chance


class SeaAnimalRaceView(discord.ui.LayoutView):
    """Lobby and race controls rendered with Components V2."""

    def __init__(self, cog: SeaAnimalRaceCommands, game: RaceGame):
        super().__init__(timeout=RACE_LOBBY_TIMEOUT)
        self.cog = cog
        self.game = game
        self.status = discord.ui.TextDisplay(self._content())
        self.footer = discord.ui.TextDisplay(self._footer_content())
        self.join = discord.ui.Button(
            label="Join",
            style=discord.ButtonStyle.secondary,
            custom_id=f"race:join:{game.channel_id}",
        )
        self.leave = discord.ui.Button(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            custom_id=f"race:leave:{game.channel_id}",
        )
        self.start = discord.ui.Button(
            label="Start",
            style=discord.ButtonStyle.secondary,
            custom_id=f"race:start:{game.channel_id}",
        )
        self.edit_bid = discord.ui.Button(
            label="Edit Bid",
            style=discord.ButtonStyle.secondary,
            custom_id=f"race:edit-bid:{game.channel_id}",
        )
        self.join.callback = self._join
        self.leave.callback = self._leave
        self.start.callback = self._start
        self.edit_bid.callback = self._edit_bid
        # Keep the game display in the CV2 container and the lobby controls at
        # message level, matching the other games' outside-the-board actions.
        self.container = discord.ui.Container(
            self.status,
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
            return "## Sea Animal Race\nThe race was cancelled."
        if self.game.finished:
            winner = self.game.winner
            result = (
                f"{winner.display_animal} **{winner.name}** wins!"
                if winner is not None
                else "The race ended without a winner."
            )
            return (
                f"## Sea Animal Race\n{race_grid(self.game)}\n"
                f"\n{result}\nPrize pool: **{self.game.pool:,} Coins**"
            )
        if not self.game.started:
            lobby = (
                "\n".join(
                    f"{p.animal} · {discord.utils.escape_markdown(p.name)}"
                    f" · {p.wager:,} Coins"
                    for p in self.game.players
                )
                or "No racers have joined yet."
            )
            return (
                "## Sea Animal Race\n"
                f"{race_grid(self.game)}\n\n"
                "The race starts in 60 seconds, or when the host presses Start.\n\n"
                f"Players ({len(self.game.players)}/{RACE_MAX_PLAYERS})\n{lobby}"
            )
        return f"## Sea Animal Race\n{race_grid(self.game)}"

    def _footer_content(self) -> str:
        footer = " · ".join(
            f"{p.animal} - {discord.utils.escape_markdown(p.name)}"
            for p in self.game.players
            if not p.is_bot
        )
        return f"-# {footer}" if footer else "-# No human racers"

    def refresh(self) -> None:
        self.status.content = self._content()
        self.footer.content = self._footer_content()
        lobby = (
            not self.game.started and not self.game.finished and not self.game.cancelled
        )
        self.join.disabled = not lobby or len(self.game.players) >= RACE_MAX_PLAYERS
        self.leave.disabled = not lobby
        self.start.disabled = not lobby or len(self.game.players) < 1
        self.edit_bid.disabled = not lobby

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id != self.game.channel_id:
            await interaction.response.send_message(
                "This race is in another channel.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def _join(self, interaction: discord.Interaction) -> None:
        await self.cog._join_race(interaction, self.game)

    async def _leave(self, interaction: discord.Interaction) -> None:
        await self.cog._leave_race(interaction, self.game)

    async def _start(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message(
                "Only the person who started the race can start it early.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.cog._begin_race(self.game, interaction=interaction)

    async def _edit_bid(self, interaction: discord.Interaction) -> None:
        if self.game.started or self.game.finished or self.game.cancelled:
            await interaction.response.send_message(
                "The race lobby is closed.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        participant = self.cog._participant_for(self.game, interaction.user.id)
        if participant is None:
            await interaction.response.send_message(
                "Join the race before editing your bid.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(
            RaceBidModal(self.cog, self.game, participant.wager)
        )

    async def on_timeout(self) -> None:
        if not self.game.started and not self.game.finished:
            await self.cog._begin_race(self.game)


class RaceBidModal(discord.ui.Modal, title="Edit race bid"):
    bid = discord.ui.TextInput(
        label="Bid amount",
        placeholder="10+ Coins",
        min_length=2,
        max_length=20,
        required=True,
    )

    def __init__(
        self,
        cog: "SeaAnimalRaceCommands",
        game: RaceGame,
        current_bid: int,
    ) -> None:
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
        if amount < RACE_MIN_BID:
            await interaction.response.send_message(
                "Bids must be at least 10 Coins.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.cog._edit_bid(interaction, self.game, amount)


class SeaAnimalRaceCommands:
    """Text ``race`` command and its persistent race/statistics helpers."""

    bot: Fishie
    _race_games: dict[int, RaceGame]

    @cast(Any, commands.group)(
        name="race", aliases=("seaanimalrace", "sea-race"), invoke_without_command=True
    )
    async def race(self, ctx: Context, *, amount: str | None = None) -> None:
        """Race sea animals for Coins."""

        await self._start_race(ctx, amount)

    @race.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def race_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Sea Animal Race wins, losses, earnings, and faints."""

        await self._send_race_stats(ctx, user)

    async def _reserve_race_wager(self, user_id: int, amount: int) -> bool:
        try:
            await self.bot.currency.debit(user_id, amount, "sea_animal_race_wager")
        except Exception:
            return False
        return True

    async def _refund_race_wager(self, user_id: int, amount: int) -> bool:
        try:
            await self.bot.currency.credit(user_id, amount, "sea_animal_race_refund")
            return True
        except Exception:
            self.bot.logger.exception("Failed to refund a Sea Animal Race wager")
            return False

    def _participant_for(self, game: RaceGame, user_id: int) -> RaceParticipant | None:
        return next(
            (p for p in game.players if p.user_id == user_id and not p.is_bot), None
        )

    async def _load_racing_emoji(self, participant: RaceParticipant) -> None:
        """Load one human's equipped racing emoji and current wallet balance.

        Race rendering should remain usable if the currency migration is being
        rolled out or an old database row is malformed.  In those cases the
        participant simply receives the normal sea-animal fallback.
        """

        if participant.is_bot or participant.racing_emoji_loaded:
            return
        participant.racing_emoji_loaded = True
        service = getattr(self.bot, "currency", None)
        lookup = getattr(service, "equipped_racing_emoji", None)
        if callable(lookup):
            try:
                row = await cast(Any, lookup)(participant.user_id)
            except Exception:
                self.bot.logger.exception(
                    "Failed to load racing emoji for user %s", participant.user_id
                )
            else:
                parsed = _racing_emoji_from_row(row) if row is not None else None
                if parsed is not None:
                    display, key, is_sea_animal = parsed
                    participant.preferred_emoji = display
                    participant.preferred_emoji_key = key
                    participant.preferred_is_sea_animal = is_sea_animal

                    get_wallet = getattr(service, "get_wallet", None)
                    if callable(get_wallet):
                        try:
                            wallet = await cast(Any, get_wallet)(participant.user_id)
                            participant.preferred_emoji_balance = int(
                                _row_value(wallet, "balance", 0) or 0
                            )
                        except Exception:
                            # A missing wallet is equivalent to a zero balance
                            # for duplicate ownership tie-breaking.
                            self.bot.logger.exception(
                                "Failed to load wallet for racing emoji user %s",
                                participant.user_id,
                            )

        # Keep all owned sea-animal identities reserved, not just the
        # currently equipped one.  This is deliberately best-effort so a
        # migration rollout or a temporarily unavailable currency service does
        # not prevent the normal race from starting.
        owned_lookup = getattr(service, "owned_racing_emojis", None)
        if callable(owned_lookup):
            try:
                owned_rows = await cast(Any, owned_lookup)(participant.user_id)
            except Exception:
                self.bot.logger.exception(
                    "Failed to load owned racing emojis for user %s",
                    participant.user_id,
                )
            else:
                for owned_row in owned_rows or ():
                    owned = _racing_emoji_from_row(owned_row)
                    if owned is None or not owned[2]:
                        continue
                    participant.reserved_sea_animal_keys.add(owned[1])

    def _new_animal(self, game: RaceGame, *, blocked: set[str] | None = None) -> str:
        """Choose a default animal without duplicating existing lanes.

        ``blocked`` contains purchased sea animals reserved by human players.
        It is intentionally separate from ``used`` because the owner of an
        equipped reserved animal is allowed to display it while every fallback
        lane is prohibited from doing so.
        """

        blocked = blocked or set()
        used = {p.animal for p in game.players if p.animal in race_animals}
        available = [
            animal
            for animal in race_animals
            if animal not in used and animal not in blocked
        ]
        if available:
            return random.choice(available)
        unblocked = [animal for animal in race_animals if animal not in blocked]
        return random.choice(unblocked or race_animals)

    async def _assign_racing_emojis(self, game: RaceGame) -> None:
        """Resolve equipped emojis and assign safe lanes for this race.

        If multiple participants equip the same emoji, the participant with
        the largest wallet balance owns that lane's display emoji.  Ties are
        deterministic (lowest user ID).  Any equipped sea animal is reserved
        for its owner and removed from the fallback pool used by other humans
        and Fishie bots.  This includes owned-but-not-equipped default sea
        animals, which are still reserved by the shop rules.
        """

        humans = [participant for participant in game.players if not participant.is_bot]
        await asyncio.gather(*(self._load_racing_emoji(p) for p in humans))

        desired: dict[tuple[str, int | str], list[RaceParticipant]] = {}
        blocked: set[str] = set()
        for participant in humans:
            for reserved_key in participant.reserved_sea_animal_keys:
                if (
                    reserved_key[0] == "unicode"
                    and str(reserved_key[1]) in race_animals
                ):
                    blocked.add(str(reserved_key[1]))
            key = participant.preferred_emoji_key
            if key is None or participant.preferred_emoji is None:
                continue
            desired.setdefault(key, []).append(participant)
            if participant.preferred_is_sea_animal:
                # Unicode sea animals are directly comparable with the
                # fallback catalog.  Custom sea-animal emoji cannot collide
                # with a Unicode fallback but remain reserved by their key.
                if key[0] == "unicode" and str(key[1]) in race_animals:
                    blocked.add(str(key[1]))

        winners: set[int] = set()
        used_defaults: set[str] = set()
        for candidates in desired.values():
            owner = max(
                candidates,
                key=lambda participant: (
                    # The wallet was read after the wager was reserved. Add
                    # that wager back so duplicate ownership is decided from
                    # the balance the user had when joining the race.
                    participant.preferred_emoji_balance + participant.wager,
                    -participant.user_id,
                ),
            )
            if owner.preferred_emoji is None:
                continue
            owner.animal = owner.preferred_emoji
            winners.add(owner.user_id)
            if owner.preferred_emoji in race_animals:
                used_defaults.add(owner.preferred_emoji)

        # Reset every non-owner before filling fallback lanes.  This avoids a
        # prior lobby assignment consuming a now-reserved animal when a richer
        # duplicate owner joins later.
        for participant in game.players:
            if participant.user_id not in winners:
                participant.animal = race_blue_square

        fallback_blocked = blocked | used_defaults
        for participant in game.players:
            if participant.user_id in winners:
                continue
            participant.animal = self._new_animal(game, blocked=fallback_blocked)

    async def _start_race(self, ctx: Context, amount: object | None) -> None:
        if not await cast(Any, self)._require_currency_tracking(ctx):
            return
        channel_id = int(ctx.channel.id)
        current = self._race_games.get(channel_id)
        if current is not None and not current.finished and not current.cancelled:
            await ctx.send(
                "There is already a Sea Animal Race in this channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        resolver = getattr(self, "_resolve_coin_amount", None)
        if callable(resolver):
            bid, stop = await cast(Any, resolver)(
                ctx,
                amount,
                default=RACE_DEFAULT_BID,
                minimum=RACE_MIN_BID,
            )
            if stop or bid is None:
                return
        else:
            # Keep this mixin usable in lightweight tests/extensions that do
            # not include Fun's shared resolver.
            try:
                from core.currency import CoinAmountError, parse_coin_amount

                parsed = parse_coin_amount(
                    RACE_DEFAULT_BID if amount is None else amount
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
        if bid < RACE_MIN_BID:
            await ctx.send(
                f"Bids must be at least {RACE_MIN_BID:,} Coins.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._reserve_race_wager(ctx.author.id, bid):
            await ctx.send(
                "You do not have enough Coins for that bid.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        participant = RaceParticipant(
            ctx.author.id,
            _safe_name(ctx.author),
            race_blue_square,
            bid,
        )
        game = RaceGame(
            ctx,
            ctx.author.id,
            channel_id,
            ctx.guild.id if ctx.guild else None,
            players=[participant],
        )
        await self._assign_racing_emojis(game)
        self._race_games[channel_id] = game
        view = SeaAnimalRaceView(self, game)
        game.view = view
        try:
            game.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            self._race_games.pop(channel_id, None)
            await self._refund_race_wager(ctx.author.id, bid)
            raise
        game.lobby_task = asyncio.create_task(self._lobby_timer(game))

    async def _lobby_timer(self, game: RaceGame) -> None:
        try:
            await asyncio.sleep(RACE_LOBBY_TIMEOUT)
            await self._begin_race(game)
        except asyncio.CancelledError:
            return
        except Exception:
            self.bot.logger.exception("Failed to start a Sea Animal Race lobby")

    async def _join_race(
        self, interaction: discord.Interaction, game: RaceGame
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                await interaction.response.send_message(
                    "This race has already started.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self._participant_for(game, interaction.user.id) is not None:
                await interaction.response.send_message(
                    "You already joined this race.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if len(game.players) >= RACE_MAX_PLAYERS:
                await interaction.response.send_message(
                    "This race is full.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not await self._reserve_race_wager(
                interaction.user.id, RACE_DEFAULT_BID
            ):
                await interaction.response.send_message(
                    "You do not have enough Coins to join.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            game.players.append(
                RaceParticipant(
                    interaction.user.id,
                    _safe_name(interaction.user),
                    race_blue_square,
                    RACE_DEFAULT_BID,
                )
            )
            await self._assign_racing_emojis(game)
            game.view.refresh() if game.view else None
            await interaction.response.edit_message(
                view=game.view, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _edit_bid(
        self, interaction: discord.Interaction, game: RaceGame, amount: int
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                await interaction.response.send_message(
                    "The race lobby is closed.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            participant = self._participant_for(game, interaction.user.id)
            if participant is None:
                await interaction.response.send_message(
                    "Join the race before editing your bid.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            difference = amount - participant.wager
            if difference > 0 and not await self._reserve_race_wager(
                participant.user_id, difference
            ):
                await interaction.response.send_message(
                    "You do not have enough Coins for that bid.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if difference < 0:
                if not await self._refund_race_wager(participant.user_id, -difference):
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

    async def _leave_race(
        self, interaction: discord.Interaction, game: RaceGame
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                await interaction.response.send_message(
                    "The lobby is no longer open.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            participant = self._participant_for(game, interaction.user.id)
            if participant is None:
                await interaction.response.send_message(
                    "You are not in this race.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            game.players.remove(participant)
            await self._refund_race_wager(participant.user_id, participant.wager)
            if not game.players:
                game.cancelled = True
                if game.lobby_task is not None:
                    game.lobby_task.cancel()
                if game.view is not None:
                    game.view.refresh()
                await interaction.response.edit_message(
                    view=game.view, allowed_mentions=discord.AllowedMentions.none()
                )
                self._race_games.pop(game.channel_id, None)
                return
            if game.view is not None:
                game.view.refresh()
            await interaction.response.edit_message(
                view=game.view, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _begin_race(
        self, game: RaceGame, *, interaction: discord.Interaction | None = None
    ) -> None:
        async with game.lock:
            if game.started or game.finished or game.cancelled:
                if interaction is not None and not interaction.response.is_done():
                    await interaction.response.send_message(
                        "This race has already started.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return
            if not game.players:
                game.cancelled = True
                return
            if (
                game.lobby_task is not None
                and game.lobby_task is not asyncio.current_task()
            ):
                game.lobby_task.cancel()
            bot_count = RACE_MAX_PLAYERS - len(game.players)
            human_bids = [
                participant.wager
                for participant in game.players
                if not participant.is_bot
            ]
            wagers = bot_wagers(
                human_bids,
                bot_count,
                minimum=RACE_MIN_BID,
                fallback_maximum=100,
            )
            names = bot_names(
                bot_count,
                guild=game.ctx.guild,
                reserved=(participant.name for participant in game.players),
            )
            for name, wager in zip(names, wagers):
                bot_number = len(game.players) + 1
                game.players.append(
                    RaceParticipant(
                        -bot_number,
                        name,
                        race_blue_square,
                        0,
                        is_bot=True,
                        bot_wager=wager,
                    )
                )
            await self._assign_racing_emojis(game)
            game.pool = sum(p.wager for p in game.players if not p.is_bot) + sum(
                p.bot_wager for p in game.players if p.is_bot
            )
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
            game.race_task = asyncio.create_task(self._race_loop(game))

    async def _race_loop(self, game: RaceGame) -> None:
        try:
            while not game.finished and not game.cancelled:
                await asyncio.sleep(RACE_TICK_SECONDS)
                async with game.lock:
                    if game.finished or game.cancelled:
                        return
                    game.tick_count += 1
                    alive = [p for p in game.players if p.alive]
                    solo_human = sum(not p.is_bot for p in alive) == 1 and len(
                        alive
                    ) == 1 + sum(p.is_bot for p in alive)
                    reached: list[RaceParticipant] = []
                    for participant in alive:
                        # Injury is checked only after a successful advance;
                        # animals that stay in place cannot faint this tick.
                        if not movement_success(
                            solo_human=solo_human, is_bot=participant.is_bot, rng=random
                        ):
                            continue
                        if injury_roll(
                            solo_human=solo_human, is_bot=participant.is_bot, rng=random
                        ):
                            participant.alive = False
                            participant.fainted = True
                            continue
                        participant.position = min(
                            RACE_COLUMNS - 1, participant.position + 1
                        )
                        if participant.position >= RACE_COLUMNS - 1:
                            reached.append(participant)
                    if reached:
                        # A tied final move is replayed until exactly one racer succeeds.
                        winner = (
                            reached[0]
                            if len(reached) == 1
                            else await self._break_tie(reached, solo_human)
                        )
                        if winner is not None:
                            for participant in reached:
                                if participant is winner:
                                    participant.winner = True
                                    participant.alive = False
                                elif (
                                    participant is not winner
                                    and not participant.fainted
                                ):
                                    participant.position = RACE_COLUMNS - 2
                            game.winner = winner
                            game.finished = True
                            await self._settle_race(game)
                            if game.view is not None:
                                game.view.refresh()
                            if game.message is not None and game.view is not None:
                                await game.message.edit(
                                    view=game.view,
                                    allowed_mentions=discord.AllowedMentions.none(),
                                )
                            return
                    if not any(p.alive for p in game.players):
                        # A simultaneous injury is extremely unlikely, but a
                        # race always needs one winner so the pool is never
                        # stranded.  Pick one of the fainted racers as the
                        # emergency winner and restore its display animal.
                        fallback = random.choice(game.players)
                        fallback.fainted = False
                        fallback.alive = False
                        fallback.winner = True
                        fallback.position = RACE_COLUMNS - 1
                        game.winner = fallback
                        game.finished = True
                        await self._settle_race(game)
                    if game.view is not None:
                        game.view.refresh()
                    if game.message is not None and game.view is not None:
                        await game.message.edit(
                            view=game.view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
        except asyncio.CancelledError:
            return
        except Exception:
            self.bot.logger.exception("Sea Animal Race loop failed")

    async def _break_tie(
        self, contenders: list[RaceParticipant], solo_human: bool
    ) -> RaceParticipant | None:
        while True:
            successes: list[RaceParticipant] = []
            for participant in contenders:
                if not participant.alive or not movement_success(
                    solo_human=solo_human, is_bot=participant.is_bot, rng=random
                ):
                    continue
                if injury_roll(
                    solo_human=solo_human, is_bot=participant.is_bot, rng=random
                ):
                    participant.alive = False
                    participant.fainted = True
                    continue
                successes.append(participant)
            if len(successes) == 1:
                return successes[0]
            if not any(participant.alive for participant in contenders):
                return None
            # A tied finish is still a race attempt: keep the same cadence as
            # normal movement instead of resolving several rolls in one
            # update.
            await asyncio.sleep(RACE_TICK_SECONDS)

    async def _settle_race(self, game: RaceGame) -> None:
        winner = game.winner
        if winner is None:
            return
        for participant in game.players:
            if participant.is_bot:
                continue
            if participant is winner:
                await self._credit_race(
                    participant.user_id, game.pool, "sea_animal_race_win"
                )
                await self._record_race_stat(
                    participant, won=True, fainted=False, earnings=game.pool
                )
            else:
                await self._record_race_stat(
                    participant,
                    won=False,
                    fainted=participant.fainted,
                    earnings=0,
                )
        if game.view is not None:
            game.view.join.disabled = True
            game.view.leave.disabled = True
            game.view.start.disabled = True

    async def _credit_race(self, user_id: int, amount: int, source: str) -> None:
        try:
            await self.bot.currency.credit(user_id, amount, source)
        except Exception:
            self.bot.logger.exception("Failed to award Sea Animal Race payout")

    async def _record_race_stat(
        self, participant: RaceParticipant, *, won: bool, fainted: bool, earnings: int
    ) -> None:
        try:
            await self.bot.pool.execute(
                """
                INSERT INTO sea_animal_race_stats
                    (user_id, wins, losses, faints, coins_earned, coins_lost)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (user_id) DO UPDATE SET
                    wins = sea_animal_race_stats.wins + EXCLUDED.wins,
                    losses = sea_animal_race_stats.losses + EXCLUDED.losses,
                    faints = sea_animal_race_stats.faints + EXCLUDED.faints,
                    coins_earned = sea_animal_race_stats.coins_earned + EXCLUDED.coins_earned,
                    coins_lost = sea_animal_race_stats.coins_lost + EXCLUDED.coins_lost,
                    updated_at = now()
                """,
                participant.user_id,
                int(won),
                int(not won),
                int(fainted),
                earnings,
                0 if won else participant.wager,
            )
        except Exception:
            self.bot.logger.exception("Failed to record Sea Animal Race statistics")

    async def _send_race_stats(
        self, ctx: Context, user: discord.User | None = None
    ) -> None:
        target = user or ctx.author
        visible = (
            target.id == ctx.author.id
            or self.bot.db_cache.game_history_visible_to(target.id, ctx.author.id)
        )
        selected = (
            await self.bot.pool.fetchrow(
                "SELECT wins, losses, faints, coins_earned, coins_lost "
                "FROM sea_animal_race_stats WHERE user_id = $1",
                target.id,
            )
            if visible
            else None
        )
        rows = await self.bot.pool.fetch(
            "SELECT user_id, wins, losses, faints, coins_earned, coins_lost "
            "FROM sea_animal_race_stats ORDER BY wins DESC, coins_earned DESC, user_id LIMIT 5"
        )
        rows = [
            row
            for row in rows
            if self.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ]
        safe = discord.utils.escape_markdown(_safe_name(target))
        values = selected or {
            "wins": 0,
            "losses": 0,
            "faints": 0,
            "coins_earned": 0,
            "coins_lost": 0,
        }
        lines = [
            f"## Sea Animal Race stats for {safe}",
            f"**Wins:** {int(values['wins']):,} · **Losses:** {int(values['losses']):,} · **Faints:** {int(values['faints']):,}",
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
                    f"**#{index} {discord.utils.escape_markdown(name)}** · {int(row['wins']):,} wins"
                )
        else:
            lines.append("No Sea Animal Races have been recorded yet.")
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=getattr(self.bot, "embedcolor", discord.Colour.blurple()),
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())
