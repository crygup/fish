from __future__ import annotations

import asyncio
import json
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

import discord
import psutil
from discord import app_commands
from discord.ext import commands, tasks

from core.currency import (
    COIN_AMOUNT_DESCRIPTION,
    EVERYTHING_AMOUNT,
    MAX_WAGER_PAYOUT,
    OPTIONAL_COIN_AMOUNT_DESCRIPTION,
    BalanceOverflow,
    CoinAmountError,
    CurrencyService,
    InsufficientFunds,
    InvalidAmount,
    award_daily_capped_coins,
    parse_coin_amount,
)
from core.handoff import is_legacy_instance
from utils import get_or_fetch_user, to_image
from utils.paths import FILES_ROOT

from .about import About
from .birthday import BirthdayCommands
from .blackjack import BlackjackCommands, BlackjackGame, BlackjackView
from .burger import BurgerCommands
from .connectfour import (
    ConnectFourChallengeView,
    ConnectFourController,
    ConnectFourSetupView,
)
from .corn import Corn
from .crash import (
    CRASH_CASHOUT_ALIASES,
    CRASH_GAME_TIMEOUT,
    CRASH_MAX_BID,
    CRASH_MIN_BID,
    CRASH_TICK_SECONDS,
    CrashGame,
    CrashView,
)
from .game_2048 import Game2048, Game2048View, highest_tile
from .helpers import RPS_ALWAYS_WIN_USER_ID, WTPView, dagpi
from .lastletter import LastLetterCommands, LastLetterGame
from .library_uploads import (
    LibraryUploadsPageSource,
    post_media_condition,
    resolve_library_filters,
    start_library_pager,
    video_media_condition,
)
from .lightsout import (
    DAILY_PAYOUT_CAP,
    STARTING_PAYOUT,
    LightsOutGame,
    LightsOutView,
    completion_payout,
)
from .luckyroll import LuckyRollCommands
from .memory import MemoryGame, MemoryView
from .mines import MAX_BOMBS, MIN_BOMBS, MinesGame, MinesView
from .minigames import (
    COLOR_MEMORIZE_DIFFICULTIES,
    COLOR_MEMORIZE_EMOJIS,
    UNSCRAMBLE_WORDS,
    ColorMemorizeGame,
    ColorMemorizeView,
    UnscrambleGame,
    UnscrambleView,
    scramble_word,
    unscramble_content,
)
from .post import PostCommands
from .pvp import (
    DUEL_MIN_BID,
    DuelBidView,
    DuelChallengeView,
    DuelChoiceView,
    account_age_message,
    account_is_old_enough,
    parse_duel_arguments,
)
from .race import SeaAnimalRaceCommands
from .reactions import ReactionStats
from .slots import SlotsCommands
from .social import SocialCommands
from .streak_games import (
    GAME_MAX_BID,
    GAME_MIN_BID,
    HeadsOrTailsGame,
    HeadsOrTailsView,
    HigherOrLowerGame,
    HigherOrLowerView,
    RockPaperScissorsGame,
    RockPaperScissorsWagerView,
    StreakGame,
)
from .tictactoe import TicTacToeController
from .upload import UploadCommands
from .video import VideoCommands
from .wordbomb import WordBombCommands
from .wordle import (
    WORDLE_WORDS,
    WordleBoardView,
    WordleGame,
    WordleSettingsView,
    get_wordle_settings,
    message_guess,
    new_wordle_game,
    record_wordle_result,
    render_wordle_board,
    save_wordle_settings,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class PhoneFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    onlyme: bool = commands.flag(
        aliases=["om", "private"],
        default=False,
        description="Only relay messages sent by you from this channel.",
    )

    @classmethod
    def parse_flags(cls, argument: str, *, ignore_extra: bool = True):
        """Allow the private flag to be used without a value."""
        bare_flag = r"(?<!\S)--?(?:onlyme|om|private)(?=\s*(?:--?\w+(?:\s|$)|$))"
        found = bool(re.search(bare_flag, argument, flags=re.IGNORECASE))
        argument = re.sub(bare_flag, "", argument, flags=re.IGNORECASE).strip()
        parsed = super().parse_flags(argument, ignore_extra=ignore_extra)
        if found:
            parsed["onlyme"] = ["true"]
        return parsed


@dataclass
class PhoneRinger:
    author_id: int
    guild_id: int
    channel_id: int
    channel: discord.abc.Messageable
    event: asyncio.Event
    onlyme: bool


@dataclass
class PhoneConnection:
    channel_ids: tuple[int, int]
    user_ids: tuple[int, int]
    onlyme: tuple[bool, bool]
    last_activity: float
    idle_task: asyncio.Task[None] | None = field(default=None, repr=False)
    logs: list[PhoneLogEntry] = field(default_factory=list, repr=False)
    log_bytes: int = field(default=0, repr=False)
    logs_truncated: bool = field(default=False, repr=False)
    cleanup_task: asyncio.Task[None] | None = field(default=None, repr=False)
    reported: bool = False


@dataclass
class PhoneLogEntry:
    guild_id: int
    username: str
    user_id: int
    message_id: int
    channel_id: int
    content: str


class PhoneConsentView(discord.ui.View):
    def __init__(self, cog: Fun, user_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        self.result: bool | None = None
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This consent prompt belongs to another user.", ephemeral=True
        )
        return False

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _submit(
        self,
        interaction: discord.Interaction,
        accepted: bool,
    ) -> None:
        try:
            await self.cog._save_phone_consent(self.user_id, accepted)
        except Exception:
            self.cog.bot.logger.exception("Failed to save phone consent")
            await interaction.response.send_message(
                "I couldn't save your phone consent. Please try again.", ephemeral=True
            )
            return

        self.result = accepted
        self.stop()
        self.disable_all()
        content = (
            "Phone consent accepted. You can now use `fish phone`."
            if accepted
            else "Phone consent declined. Your phone messages will not be relayed."
        )
        await interaction.response.edit_message(content=content, view=self)

    @discord.ui.button(label="I Agree", style=discord.ButtonStyle.green)
    async def accept(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._submit(interaction, True)

    @discord.ui.button(label="I Don't Agree", style=discord.ButtonStyle.red)
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._submit(interaction, False)

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PhoneReportView(discord.ui.View):
    def __init__(self, cog: Fun, connection: PhoneConnection):
        super().__init__(timeout=300)
        self.cog = cog
        self.connection = connection
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.connection.user_ids:
            return True
        await interaction.response.send_message(
            "Only someone who was in this call can report it.", ephemeral=True
        )
        return False

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(label="Report conversation", style=discord.ButtonStyle.red)
    async def report(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        try:
            sent = await self.cog._report_phone_session(self.connection)
        except Exception:
            self.cog.bot.logger.exception("Failed to send phone report")
            await interaction.response.send_message(
                "I couldn't send the report. Please try again.", ephemeral=True
            )
            return

        self.disable_all()
        if sent:
            content = "Phone report sent. Thank you."
        else:
            content = "This phone call has already been reported."
        await interaction.response.edit_message(content=content, view=self)

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ClickView(discord.ui.LayoutView):
    """Components V2 click counter that accepts clicks from any user."""

    def __init__(self, cog: "Fun", guild_id: int | None, total: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.total = total
        self.counter = discord.ui.TextDisplay(self._counter_text())
        self.button = discord.ui.Button(
            label="Click",
            style=discord.ButtonStyle.primary,
        )
        self.button.callback = self._click
        self.add_item(
            discord.ui.Container(
                self.counter,
                discord.ui.Separator(),
                discord.ui.ActionRow(self.button),
                accent_color=self.cog.bot.embedcolor,
            )
        )

    def _counter_text(self) -> str:
        return f"## Click\n**Global clicks:** {self.total:,}"

    async def _click(self, interaction: discord.Interaction) -> None:
        try:
            self.total = await self.cog._record_click(
                interaction.user.id, self.guild_id
            )
        except Exception:
            self.cog.bot.logger.exception("Failed to record click")
            await interaction.response.send_message(
                "I couldn't record that click. Please try again.", ephemeral=True
            )
            return

        self.counter.content = self._counter_text()
        await interaction.response.edit_message(view=self)


PHONE_IDLE_TIMEOUT = 60.0
PHONE_LOG_MAX_ENTRIES = 500
PHONE_LOG_MAX_BYTES = 200_000

# These commands remain owned by the Fun cog so they can share the active
# game controllers, but the help UI presents them under its virtual Games
# category.  Keeping this metadata on the command also lets the website/API
# use the same category without duplicating command registration.
GAMES_HELP_COMMANDS = frozenset(
    {
        "game",
        "click",
        "dice",
        "8ball",
        "2048",
        "lightsout",
        "higher-or-lower",
        "heads-or-tails",
        "wordle",
        "memory",
        "rock-paper-scissors",
        "unscramble",
        "color",
        "wordbomb",
        "lastletter",
        "lastl",
        "last-letter",
        "race",
        "luckyroll",
        "slots",
        "mines",
        "blackjack",
        "crash",
        "tictactoe",
        "connect4",
        # Names used by the slash-only children of ``/game``.
        "tic-tac-toe",
        "connect-four",
        "color-memorize",
        "lights-out",
    }
)


class Fun(
    UploadCommands,
    PostCommands,
    VideoCommands,
    BurgerCommands,
    SocialCommands,
    BirthdayCommands,
    About,
    Corn,
    ReactionStats,
    SeaAnimalRaceCommands,
    LuckyRollCommands,
    SlotsCommands,
    BlackjackCommands,
    WordBombCommands,
    LastLetterCommands,
):
    """Random commands for when you're bored"""

    emoji = discord.PartialEmoji(name="\U0001f604")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        for command in self.__cog_commands__:
            if command.name in GAMES_HELP_COMMANDS:
                command.extras["help_category"] = "Games"
        self._phone_lock = asyncio.Lock()
        self._phone_ringers: list[PhoneRinger] = []
        self._phone_connections: dict[int, PhoneConnection] = {}
        self._phone_archives: dict[int, PhoneConnection] = {}
        self._phone_consent_lock = asyncio.Lock()
        self._phone_consent_cache: dict[int, bool | None] = {}
        self._phone_consent_prompts: set[int] = set()
        self._color_memorize_games: dict[int, ColorMemorizeGame] = {}
        self._unscramble_games: dict[int, UnscrambleGame] = {}
        self._2048_games: dict[int, Game2048] = {}
        self._lightsout_games: dict[int, LightsOutGame] = {}
        self._higher_or_lower_games: dict[int, HigherOrLowerGame] = {}
        self._heads_or_tails_games: dict[int, HeadsOrTailsGame] = {}
        self._rock_paper_scissors_games: dict[int, RockPaperScissorsGame] = {}
        self._wordle_games: dict[tuple[int, int], WordleGame] = {}
        self._memory_games: dict[int, MemoryGame] = {}
        self._wordbomb_games: dict[int, Any] = {}
        self._lastletter_games: dict[int, LastLetterGame] = {}
        self._race_games: dict[int, Any] = {}
        self._luckyroll_games: dict[int, Any] = {}
        self._slots_games: dict[int, Any] = {}
        self._mines_games: dict[int, MinesGame] = {}
        self._blackjack_games: dict[int, Any] = {}
        # Player-vs-player Blackjack sessions are keyed by both participant
        # IDs.  They are kept separate from the solo house games so either
        # mode may evolve without changing the legacy model/API.
        self._blackjack_duels: dict[frozenset[int], BlackjackGame] = {}
        self._blackjack_duel_wagers: dict[frozenset[int], dict[int, list[int]]] = {}
        self._crash_games: dict[int, CrashGame] = {}
        self._pvp_duels: dict[frozenset[int], DuelChoiceView] = {}
        # Clicks are intentionally batched.  The click board is a high-volume
        # interaction and writing four counter tables (plus a currency reward)
        # for every button press needlessly contends with normal commands.
        self._click_cache_lock = asyncio.Lock()
        self._click_cache_ready = False
        self._click_cached_total = 0
        self._click_pending_total = 0
        self._click_pending_users: defaultdict[int, int] = defaultdict(int)
        self._click_pending_guilds: defaultdict[int, int] = defaultdict(int)
        self._click_pending_user_guilds: defaultdict[tuple[int, int], int] = (
            defaultdict(int)
        )
        self._click_pending_rewards: defaultdict[tuple[int, date], int] = defaultdict(
            int
        )
        self.phone_logs = discord.Webhook.from_url(
            self.bot.config["webhooks"]["phone_logs"], session=self.bot.session
        )
        self.process = psutil.Process()
        application_id = getattr(self.bot, "active_application_id", None)
        if callable(application_id):
            application_id = application_id()
        if application_id is None:
            application_id = self.bot.config["ids"]["bot_id"]
        self.invite_url = discord.utils.oauth_url(
            int(cast(Any, application_id)), permissions=self.bot.bot_permissions
        )
        self._tictactoe_controller = TicTacToeController(self)
        self._connectfour_controller = ConnectFourController(self)

    async def _require_currency_tracking(self, ctx: Context) -> bool:
        """Return whether a currency-using game may be started for *ctx*."""
        checker = getattr(self.bot.db_cache, "user_currency_tracking_enabled", None)
        if not callable(checker) or checker(ctx.author.id):
            return True
        await ctx.send(
            "Currency tracking is disabled. Enable it from `settings tracking` "
            "before using Coin-based games.",
            ephemeral=ctx.interaction is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def _currency_tracking_enabled(self, user_id: int) -> bool:
        """Return whether a participant permits Coin ledger activity."""

        checker = getattr(self.bot.db_cache, "user_currency_tracking_enabled", None)
        return bool(checker(user_id)) if callable(checker) else True

    async def _resolve_coin_amount(
        self,
        ctx: Context,
        raw: object,
        *,
        default: int | None = None,
        minimum: int = GAME_MIN_BID,
    ) -> tuple[int | None, bool]:
        """Resolve a user-facing Coin expression before starting a game.

        The second return value is ``True`` when the command should stop (a
        malformed expression, an unavailable wallet, or a cancelled
        ``everything`` confirmation).  Keeping this at the Fun-cog boundary
        lets every game share the same parser while retaining each game's
        operation-specific minimum and wager validation.
        """

        if raw is None:
            return default, False
        try:
            parsed = parse_coin_amount(raw)
        except CoinAmountError as error:
            await ctx.send(str(error), allowed_mentions=discord.AllowedMentions.none())
            return None, True
        if parsed == EVERYTHING_AMOUNT:
            try:
                wallet = await self.bot.currency.get_wallet(ctx.author.id)
                balance = int(wallet.balance)
            except Exception:
                self.bot.logger.exception("Failed to resolve an everything Coin bid")
                await ctx.send(
                    "Your wallet is unavailable right now.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return None, True
            if balance < minimum:
                await ctx.send(
                    f"You need at least {minimum:,} Coins to bid everything.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return None, True
            confirmation = await ctx.prompt(
                "Are you sure you want to bid everything?",
                confirm_label="Yes",
                cancel_label="No",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if confirmation is None:
                return None, True
            return balance, False
        return int(parsed), False

    async def _send_uploads(self, ctx: Context, value: str | None = None) -> None:
        """Browse the combined approved video, image, and GIF libraries."""

        filters = await resolve_library_filters(ctx, value)
        rows: list[dict[str, Any]] = []

        async def fetch_rows(query: str, media_kind: str, *, condition: str) -> None:
            if filters.all_users:
                result = await self.bot.pool.fetch(
                    query + condition + " ORDER BY library_id"
                )
            else:
                result = await self.bot.pool.fetch(
                    query + condition + " AND uploader_id = $1 ORDER BY library_id",
                    filters.user_id,
                )
            for row in result:
                item = dict(row)
                item["media_kind"] = media_kind
                rows.append(item)

        if filters.media != "video":
            await fetch_rows(
                "SELECT id, library_id, source_url, filename, review_message_id, "
                "uploader_id FROM post_uploads WHERE status = 'approved' "
                "AND library_id IS NOT NULL",
                "post",
                condition=post_media_condition(filters.media, alias="post_uploads"),
            )
        if filters.media != "image" and filters.media != "gif":
            await fetch_rows(
                "SELECT id, library_id, source_url, filename, review_message_id, "
                "uploader_id FROM video_uploads WHERE status = 'approved' "
                "AND library_id IS NOT NULL",
                "video",
                condition=video_media_condition(filters.media, alias="video_uploads"),
            )

        rows.sort(
            key=lambda row: (
                int(row.get("library_id") or row["id"]),
                str(row.get("media_kind") or ""),
            )
        )
        if filters.all_users:
            title = "All Fishie uploads"
        elif filters.user_id == ctx.author.id:
            title = "Your Fishie uploads"
        else:
            title = "Fishie uploads"
        source = LibraryUploadsPageSource(self, ctx, rows, title=title)
        await start_library_pager(
            source,
            ctx=ctx,
            accent_color=self.bot.embedcolor,
        )

    @cast(Any, commands.command)(name="uploads")
    async def uploads(self, ctx: Context, *, filters: str | None = None) -> None:
        """Browse approved Fishie uploads with optional media and user filters."""

        async with ctx.typing():
            await self._send_uploads(ctx, filters)

    async def _click_total(self) -> int:
        """Return the global click total, including unflushed clicks.

        The first call loads the durable baseline.  Subsequent calls only read
        memory, which keeps the public click board responsive while a flush is
        pending.
        """

        async with self._click_cache_lock:
            if not self._click_cache_ready:
                value = await self.bot.pool.fetchval(
                    "SELECT clicks FROM click_totals WHERE id = TRUE"
                )
                self._click_cached_total = int(value or 0)
                self._click_cache_ready = True
            return self._click_cached_total

    async def _record_click(self, user_id: int, guild_id: int | None) -> int:
        """Record a click in memory and return the current global total.

        ``flush_click_cache`` persists the deltas in one transaction roughly
        once a minute.  A user's game-tracking opt-out still excludes the
        click from every counter and from the silent Coin reward.
        """

        if not self.bot.db_cache.user_game_tracking_enabled(user_id):
            # Keep the public board usable without writing a user's activity
            # after they disable game tracking.
            return await self._click_total()

        async with self._click_cache_lock:
            if not self._click_cache_ready:
                value = await self.bot.pool.fetchval(
                    "SELECT clicks FROM click_totals WHERE id = TRUE"
                )
                self._click_cached_total = int(value or 0)
                self._click_cache_ready = True
            self._click_cached_total += 1
            self._click_pending_total += 1
            self._click_pending_users[int(user_id)] += 1
            if guild_id is not None:
                guild_id = int(guild_id)
                self._click_pending_guilds[guild_id] += 1
                self._click_pending_user_guilds[(int(user_id), guild_id)] += 1
            # Currency is independently opt-out-able.  Keep recording the
            # click itself when game tracking is enabled, but do not create a
            # wallet/reward ledger entry for users who disabled Coins.
            currency_tracking = getattr(
                self.bot.db_cache, "user_currency_tracking_enabled", None
            )
            if not callable(currency_tracking) or currency_tracking(user_id):
                # Keep the in-memory reward bounded.  The durable daily-reward
                # row is the final authority when this is flushed.
                reward_key = (int(user_id), datetime.now(timezone.utc).date())
                if self._click_pending_rewards[reward_key] < 10_000:
                    self._click_pending_rewards[reward_key] += 1
            return self._click_cached_total

    async def flush_click_rewards(self, user_id: int | None = None) -> None:
        """Flush pending silent click rewards to the daily-capped ledger.

        Currency commands can call this before reading or changing a wallet so
        a just-earned click reward is visible immediately.  If no user is
        supplied, all pending rewards are flushed by the periodic counter
        writer.
        """

        async with self._click_cache_lock:
            if user_id is None:
                pending = dict(self._click_pending_rewards)
                self._click_pending_rewards.clear()
            else:
                uid = int(user_id)
                pending = {
                    key: amount
                    for key, amount in self._click_pending_rewards.items()
                    if key[0] == uid
                }
                for key in pending:
                    self._click_pending_rewards.pop(key, None)
        if not pending:
            return
        failed: dict[tuple[int, date], int] = {}
        pending_items = list(pending.items())
        for index, ((uid, period), amount) in enumerate(pending_items):
            try:
                await CurrencyService(self.bot.pool).award_daily_capped(
                    uid,
                    amount,
                    10_000,
                    "click",
                    now=datetime.combine(period, time.min, tzinfo=timezone.utc),
                )
            except asyncio.CancelledError:
                # A cog reload/shutdown can cancel this task while an award is
                # in flight.  Requeue this and all not-yet-attempted entries;
                # the completed entries are durable and must not be repeated.
                failed.update(dict(pending_items[index:]))
                async with self._click_cache_lock:
                    for key, value in failed.items():
                        self._click_pending_rewards[key] += value
                raise
            except Exception:
                failed[(uid, period)] = amount
                self.bot.logger.exception(
                    "Failed to flush click Coin reward for user %s", uid
                )
        if failed:
            async with self._click_cache_lock:
                for key, amount in failed.items():
                    self._click_pending_rewards[key] += amount

    async def flush_click_cache(self) -> None:
        """Persist counter deltas and silent click rewards as one batch."""

        async with self._click_cache_lock:
            if not self._click_cache_ready:
                # There cannot be deltas before the first click, but loading
                # the baseline here makes shutdown/reload safe and cheap.
                value = await self.bot.pool.fetchval(
                    "SELECT clicks FROM click_totals WHERE id = TRUE"
                )
                self._click_cached_total = int(value or 0)
                self._click_cache_ready = True
            pending_total = self._click_pending_total
            pending_users = dict(self._click_pending_users)
            pending_guilds = dict(self._click_pending_guilds)
            pending_user_guilds = dict(self._click_pending_user_guilds)
            self._click_pending_total = 0
            self._click_pending_users.clear()
            self._click_pending_guilds.clear()
            self._click_pending_user_guilds.clear()
        try:
            if pending_total or pending_users or pending_guilds or pending_user_guilds:
                async with self.bot.pool.acquire() as connection:
                    async with connection.transaction():
                        if pending_total:
                            await connection.execute(
                                "INSERT INTO click_totals (id, clicks) VALUES "
                                "(TRUE, $1) ON CONFLICT (id) DO UPDATE SET "
                                "clicks = click_totals.clicks + EXCLUDED.clicks",
                                pending_total,
                            )
                        if pending_users:
                            await connection.executemany(
                                "INSERT INTO click_user_totals (user_id, clicks) "
                                "VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE "
                                "SET clicks = click_user_totals.clicks + EXCLUDED.clicks",
                                pending_users.items(),
                            )
                        if pending_guilds:
                            await connection.executemany(
                                "INSERT INTO click_guild_totals (guild_id, clicks) "
                                "VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE "
                                "SET clicks = click_guild_totals.clicks + EXCLUDED.clicks",
                                pending_guilds.items(),
                            )
                        if pending_user_guilds:
                            await connection.executemany(
                                "INSERT INTO click_user_guild_totals "
                                "(user_id, guild_id, clicks) VALUES ($1, $2, $3) "
                                "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
                                "clicks = click_user_guild_totals.clicks + "
                                "EXCLUDED.clicks",
                                (
                                    (user_id, guild_id, amount)
                                    for (
                                        user_id,
                                        guild_id,
                                    ), amount in pending_user_guilds.items()
                                ),
                            )
        except asyncio.CancelledError:
            async with self._click_cache_lock:
                self._click_pending_total += pending_total
                for uid, amount in pending_users.items():
                    self._click_pending_users[uid] += amount
                for gid, amount in pending_guilds.items():
                    self._click_pending_guilds[gid] += amount
                for key, amount in pending_user_guilds.items():
                    self._click_pending_user_guilds[key] += amount
            raise
        except Exception:
            async with self._click_cache_lock:
                self._click_pending_total += pending_total
                for uid, amount in pending_users.items():
                    self._click_pending_users[uid] += amount
                for gid, amount in pending_guilds.items():
                    self._click_pending_guilds[gid] += amount
                for key, amount in pending_user_guilds.items():
                    self._click_pending_user_guilds[key] += amount
            self.bot.logger.exception("Failed to flush click counters")
        await self.flush_click_rewards()

    @tasks.loop(minutes=1)
    async def flush_click_cache_loop(self) -> None:
        await self.flush_click_cache()

    @flush_click_cache_loop.before_loop
    async def before_flush_click_cache_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_click_stats(self, ctx: Context, user: discord.User) -> None:
        # Include interactions that arrived since the last periodic flush in
        # leaderboard reads.
        await self.flush_click_cache()
        viewer_id = ctx.author.id
        user_visible = self.bot.db_cache.game_history_visible_to(user.id, viewer_id)
        if not user_visible:
            await ctx.send(
                "That user's click history is private.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        user_row = await self.bot.pool.fetchrow(
            "SELECT clicks FROM click_user_totals WHERE user_id = $1", user.id
        )
        global_total = await self._click_total()
        user_rows = await self.bot.pool.fetch(
            "SELECT user_id, clicks FROM click_user_totals "
            "ORDER BY clicks DESC, user_id ASC LIMIT 10"
        )
        user_rows = [
            row
            for row in user_rows
            if self.bot.db_cache.game_history_visible_to(int(row["user_id"]), viewer_id)
        ]
        guild_rows = await self.bot.pool.fetch(
            "SELECT guild_id, clicks FROM click_guild_totals "
            "ORDER BY clicks DESC, guild_id ASC LIMIT 10"
        )

        safe_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(user.name)
        )
        lines = [
            f"## Click stats for {safe_name}",
            f"**Clicks:** {int(user_row['clicks']) if user_row else 0:,}",
            f"**Global clicks:** {global_total:,}",
            "### Top users",
        ]
        if user_rows:
            for index, row in enumerate(user_rows, start=1):
                listed_user = await get_or_fetch_user(self.bot, int(row["user_id"]))
                listed_name = getattr(listed_user, "name", None) or str(row["user_id"])
                lines.append(
                    f"**#{index} {discord.utils.escape_markdown(listed_name)}** \u00b7 {int(row['clicks']):,}"
                )
        else:
            lines.append("No user clicks have been recorded yet.")

        lines.append("### Top guilds")
        if guild_rows:
            for index, row in enumerate(guild_rows, start=1):
                guild_id = int(row["guild_id"])
                guild = self.bot.get_guild(guild_id)
                guild_name = guild.name if guild is not None else f"Guild {guild_id}"
                lines.append(
                    f"**#{index} {discord.utils.escape_markdown(guild_name)}** \u00b7 {int(row['clicks']):,}"
                )
        else:
            lines.append("No guild clicks have been recorded yet.")

        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(
        name="click",
        aliases=("clicks",),
        invoke_without_command=True,
    )
    async def click(self, ctx: Context) -> None:
        """Show a button that increments the global click counter."""
        await self._start_click(ctx)

    @click.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def click_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show a user's clicks and the global user and guild leaderboards."""
        await self._send_click_stats(ctx, user)

    async def _start_click(self, ctx: Context) -> None:
        view = ClickView(
            self,
            ctx.guild.id if ctx.guild is not None else None,
            await self._click_total(),
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @cast(Any, commands.hybrid_group)(
        name="game",
        invoke_without_command=True,
        fallback="search",
    )
    @app_commands.describe(query="Steam game name, app ID, or Steam store URL.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game(
        self,
        ctx: Context,
        *,
        query: str | None = commands.param(
            default=None,
            description="Steam game name, app ID, or Steam store URL.",
        ),
    ) -> None:
        """Search for a Steam game, or show Fishie's games."""
        if not query or not query.strip():
            await ctx.send_help(ctx.command)
            return

        # Search is the registered cog that owns the Steam mixin; Steam is
        # not registered as a standalone cog.
        steam = self.bot.get_cog("Search")
        if steam is None:
            raise commands.BadArgument("Steam game search is unavailable right now.")
        async with ctx.typing():
            game_data, reviews = await steam._get_game(query)  # type: ignore[attr-defined]
            if game_data is None:
                raise commands.BadArgument(
                    f"No Steam game found for **{query.strip()}**."
                )
            app_id = int(game_data.get("steam_appid") or game_data.get("appid") or 0)
            playtime = (
                await steam._get_linked_game_playtime(ctx.author.id, app_id)  # type: ignore[attr-defined]
                if app_id
                else None
            )
            view = steam._game_view(  # type: ignore[attr-defined]
                game_data, reviews or {}, playtime
            )
            await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @game.command(name="tic-tac-toe")
    @app_commands.describe(user="The user to challenge. Omit this to play Fishie.")
    @app_commands.describe(amount=OPTIONAL_COIN_AMOUNT_DESCRIPTION)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_tic_tac_toe(
        self,
        ctx: Context,
        user: discord.User | None = None,
        amount: str | None = commands.param(
            default=None,
            description=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Play Tic-Tac-Toe against another user or Fishie."""
        arguments: list[object] = []
        if user is not None:
            arguments.append(user)
        if amount is not None:
            arguments.append(amount)
        await cast(Any, self.tictactoe)(ctx, *arguments)

    @game.command(name="connect-four", aliases=("connect4", "c4", "connect"))
    @app_commands.describe(user="The user to challenge. Omit this to play Fishie.")
    @app_commands.describe(amount=OPTIONAL_COIN_AMOUNT_DESCRIPTION)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_connect_four(
        self,
        ctx: Context,
        user: discord.User | None = None,
        amount: str | None = commands.param(
            default=None,
            description=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Play Connect Four against another user or Fishie."""
        arguments: list[object] = []
        if user is not None:
            arguments.append(user)
        if amount is not None:
            arguments.append(amount)
        await cast(Any, self.connectfour)(ctx, *arguments)

    @game.command(name="race", aliases=("sea-race", "seaanimalrace"))
    @app_commands.describe(amount=COIN_AMOUNT_DESCRIPTION)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_race(
        self,
        ctx: Context,
        amount: str | None = commands.param(
            default=None,
            description=COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Race sea animals for Coins."""
        await self._start_race(ctx, amount)

    @game.command(name="luckyroll", aliases=("lucky-roll", "lr"))
    @app_commands.describe(amount=COIN_AMOUNT_DESCRIPTION)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_luckyroll(
        self,
        ctx: Context,
        amount: str | None = commands.param(
            default=None,
            description=COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Roll dice against other players for Coins."""
        await self._start_luckyroll(ctx, amount)

    @game.command(name="slots", aliases=("slot",))
    @app_commands.describe(
        amount=COIN_AMOUNT_DESCRIPTION,
        count="Number of spins to run together, from 1 to 10.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_slots(
        self,
        ctx: Context,
        amount: str = commands.param(
            default="100",
            description=f"{COIN_AMOUNT_DESCRIPTION[:-1]} Defaults to 100.",
        ),
        count: int = commands.param(
            default=1,
            description="Number of spins to run together, from 1 to 10.",
        ),
    ) -> None:
        """Spin one or more weighted three-reel slots for Coins."""
        await self._start_slots(ctx, amount, count)

    @game.command(name="unscramble")
    @app_commands.describe(difficulty="Difficulty: easy, normal, hard, or random.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def game_unscramble(
        self,
        ctx: Context,
        difficulty: str = commands.param(
            default="random",
            description="Difficulty: easy, normal, hard, or random.",
        ),
    ) -> None:
        """Unscramble a word before the 60 second timer expires."""
        await self._start_unscramble(ctx, difficulty)

    @game.command(name="color-memorize")
    @app_commands.describe(
        difficulty="Difficulty: easy, normal, hard, extreme, or impossible."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_color_memorize(
        self,
        ctx: Context,
        difficulty: str = commands.param(
            default="normal",
            description="Difficulty: easy, normal, hard, extreme, or impossible.",
        ),
    ) -> None:
        """Memorize a flashing sequence of colors."""
        await self._start_color_memorize(ctx, difficulty)

    @game.command(name="click", aliases=("clicks",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_click(self, ctx: Context) -> None:
        """Show a button that increments the global click counter."""
        await self._start_click(ctx)

    @game.command(name="dice", aliases=("roll",))
    @app_commands.describe(
        sides="Number of sides on each die, from 2 to 1,000,000.",
        rolls="Number of dice to roll, from 1 to 10.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_dice(
        self,
        ctx: Context,
        sides: int = commands.param(
            default=6, description="Number of sides on each die (defaults to 6)."
        ),
        rolls: int = commands.param(
            default=1, description="Number of dice to roll, up to 10."
        ),
    ) -> None:
        """Roll one or more dice."""
        await self._roll_dice(ctx, sides, rolls)

    @game.command(name="8ball")
    @app_commands.describe(question="Question to ask the magic 8-ball.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_8ball(
        self,
        ctx: Context,
        *,
        question: str = commands.param(
            displayed_name="question", description="What shall you ask?"
        ),
    ) -> None:
        """Ask the magic 8-ball a question."""
        await self._ask_eight_ball(ctx, question)

    @commands.command(name="2048", aliases=("twentyfortyeight",))
    async def twenty_forty_eight(self, ctx: Context) -> None:
        """Play a solo game of 2048 on a four by four board."""
        await self._start_2048(ctx)

    @game.command(name="2048", aliases=("twentyfortyeight",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_2048(self, ctx: Context) -> None:
        """Play a solo game of 2048 on a four by four board."""
        await self._start_2048(ctx)

    @game.command(name="lights-out", aliases=("lightsout", "lights"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_lights_out(self, ctx: Context) -> None:
        """Turn off every light in a randomized five by five puzzle."""
        await self._start_lightsout(ctx)

    @commands.command(
        name="mines",
        aliases=("mine",),
        extras={"usage": "[amount] [bombs]"},
    )
    async def mines(self, ctx: Context, *arguments: str) -> None:
        """Play Mines with a Coin wager and one to twenty-four mines."""
        amount = "100"
        bombs = 3
        tokens = [token for token in arguments if str(token).strip()]
        # The text command historically accepts ``amount bombs``. Keep that
        # order while joining amount tokens so ``1 hundred thousand 5`` stays
        # a valid expression.
        if tokens:
            if len(tokens) > 1:
                try:
                    candidate = int(tokens[-1].replace(",", ""))
                except (TypeError, ValueError):
                    candidate = None
                if candidate is not None and 1 <= candidate <= MAX_BOMBS:
                    bombs = candidate
                    tokens.pop()
            amount = " ".join(tokens) or amount
        await self._start_mines(ctx, amount, bombs)

    @game.command(name="mines", aliases=("mine",))
    @app_commands.describe(
        amount=COIN_AMOUNT_DESCRIPTION,
        bombs="Number of mines, from 1 to 24 (defaults to 3).",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_mines(
        self,
        ctx: Context,
        amount: str = commands.param(
            default="100",
            description=f"{COIN_AMOUNT_DESCRIPTION[:-1]} Defaults to 100.",
        ),
        bombs: int = commands.param(
            default=3,
            description="Number of mines, from 1 to 24 (defaults to 3).",
        ),
    ) -> None:
        """Play Mines with a Coin wager and one to twenty-four mines."""
        await self._start_mines(ctx, amount, bombs)

    @game.command(name="blackjack", aliases=("black-jack", "bj"))
    @app_commands.describe(
        user="The player to challenge. Omit this to play against Fishie.",
        amount=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_blackjack(
        self,
        ctx: Context,
        user: discord.User | None = commands.param(
            default=None,
            description="The player to challenge. Omit this to play Fishie.",
        ),
        amount: str | None = commands.param(
            default=None,
            description=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Play free Blackjack against Fishie or challenge another player."""
        arguments: list[object] = []
        if user is not None:
            arguments.append(user)
        if amount is not None:
            arguments.append(amount)
        await self._blackjack_entry(ctx, *arguments)

    async def _blackjack_entry(self, ctx: Context, *arguments: object) -> None:
        """Dispatch a Blackjack command to a free/coin or player game.

        Text invocations may put the target and bid in either order (for
        example ``blackjack @user 100`` or ``blackjack 100 @user``).  Slash
        invocations pass the converted options through this same path so all
        validation remains consistent.
        """

        user, amount = await parse_duel_arguments(ctx, arguments)
        bot_id = (
            ctx.bot.user.id
            if ctx.bot.user is not None
            else int(ctx.bot.config["ids"]["bot_id"])
        )
        if user is None:
            # A missing amount is deliberately a free game.  Supplying an
            # amount still allows the normal house game to be wagered; the
            # default is free rather than forcing every player to stake Coins.
            await self._start_blackjack(ctx, amount)
            return
        if user.id == bot_id:
            # Fishie is available as the house only when no opponent is
            # supplied.  Player-vs-player Blackjack is strictly a two-human
            # duel; accepting Fishie as a participant would silently turn it
            # into a three-hand round.
            await ctx.send(
                "Blackjack duels are limited to two human players. "
                "Omit the opponent to play against Fishie.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if user.id == ctx.author.id:
            await ctx.send("You cannot play Blackjack against yourself.")
            return
        if user.bot:
            await ctx.send("You cannot challenge another bot to Blackjack.")
            return
        if not account_is_old_enough(ctx.author):
            await ctx.send(account_age_message(ctx.author))
            return
        if not account_is_old_enough(user):
            await ctx.send(account_age_message(user))
            return
        if amount is not None and amount < DUEL_MIN_BID:
            await ctx.send(
                f"Player bids must be at least **{DUEL_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._challenge_blackjack(ctx, user, amount)

    async def _challenge_blackjack(
        self,
        ctx: Context,
        opponent: discord.abc.User,
        amount: int | None,
    ) -> None:
        """Send a free acceptance or per-player bid confirmation for a duel."""

        async def start(
            interaction: discord.Interaction, stakes: tuple[int, int] | None
        ) -> None:
            await self._start_blackjack_duel(
                ctx, opponent, stakes=stakes, interaction=interaction
            )

        if amount is None:

            async def accept(interaction: discord.Interaction) -> None:
                await start(interaction, None)

            view: Any = DuelChallengeView(
                ctx,
                ctx.author,
                opponent,
                game_name="Blackjack",
                on_accept=accept,
            )
            content = (
                f"{opponent.mention}, you were challenged to a Blackjack duel by "
                f"**{discord.utils.escape_markdown(ctx.author.name)}**. Do you want to play?"
            )
        else:

            async def ready(
                interaction: discord.Interaction, stakes: tuple[int, int]
            ) -> None:
                await start(interaction, stakes)

            view = DuelBidView(
                ctx,
                ctx.author,
                opponent,
                game_name="Blackjack",
                challenger_bid=amount,
                on_ready=ready,
            )
            content = None
        if isinstance(view, discord.ui.LayoutView):
            setattr(
                view,
                "message",
                await ctx.send(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
            )
        else:
            setattr(
                view,
                "message",
                await ctx.send(
                    content,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
            )

    async def _load_blackjack_avatars(
        self,
        ctx: Context,
        users: tuple[discord.abc.User, ...],
    ) -> dict[int, bytes]:
        """Load the two PvP portraits once for the composite card board."""

        session = getattr(ctx, "session", None)
        if session is None:
            return {}

        async def load(user: discord.abc.User) -> tuple[int, bytes | None]:
            avatar = getattr(user, "display_avatar", None)
            if avatar is None:
                return int(user.id), None
            try:
                avatar_url = str(avatar.replace(format="png", size=128).url)
            except (AttributeError, TypeError):
                avatar_url = str(getattr(avatar, "url", ""))
            if not avatar_url:
                return int(user.id), None
            try:
                data = await to_image(session, avatar_url, bytes=True)
            except Exception:
                return int(user.id), None
            return int(user.id), data if isinstance(data, bytes) else None

        loaded = await asyncio.gather(*(load(user) for user in users))
        return {user_id: data for user_id, data in loaded if data is not None}

    async def _start_blackjack_duel(
        self,
        ctx: Context,
        opponent: discord.abc.User,
        *,
        stakes: tuple[int, int] | None,
        interaction: discord.Interaction,
    ) -> None:
        """Reserve optional wagers and replace the challenge with the game UI."""

        key = frozenset((int(ctx.author.id), int(opponent.id)))
        if key in self._blackjack_duels and not self._blackjack_duels[key].finished:
            await interaction.followup.send(
                "One of those users already has a Blackjack duel in progress.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not interaction.response.is_done():
            await interaction.response.defer()

        wager_ids: dict[int, list[int]] = {
            int(ctx.author.id): [],
            int(opponent.id): [],
        }
        stake_by_player: dict[int, int] = {}
        if stakes is not None:
            if len(stakes) != 2 or any(int(stake) < DUEL_MIN_BID for stake in stakes):
                await interaction.followup.send(
                    f"Each player bid must be at least {DUEL_MIN_BID:,} Coins.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            stake_by_player = {
                int(ctx.author.id): int(stakes[0]),
                int(opponent.id): int(stakes[1]),
            }
            if not all(
                self._currency_tracking_enabled(user_id)
                for user_id in (ctx.author.id, opponent.id)
            ):
                await interaction.followup.send(
                    "Both players must have currency tracking enabled to place a bid.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                for user_id, player_stake in stake_by_player.items():
                    wager = await self.bot.currency.open_wager(
                        user_id, player_stake, source="pvp_blackjack"
                    )
                    wager_ids[user_id].append(wager.id)
            except Exception as error:
                for user_id, ids in wager_ids.items():
                    for wager_id in ids:
                        try:
                            await self.bot.currency.settle_wager(
                                wager_id,
                                stake_by_player[user_id],
                                track_stats=False,
                            )
                        except Exception:
                            self.bot.logger.exception(
                                "Failed to refund Blackjack duel wager"
                            )
                await interaction.followup.send(
                    f"I couldn't reserve both bids: {error}",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        avatar_data = await self._load_blackjack_avatars(
            ctx,
            (ctx.author, opponent),
        )
        game = BlackjackGame.new(
            int(ctx.author.id),
            int(stake_by_player.get(int(ctx.author.id), 0)),
            0,
            # Keep the challenger as the dealer-side hand. The opponent acts
            # first; once they finish, the challenger gets Hit/Stand choices.
            # Fishie is never inserted as a participant in PvP Blackjack.
            house_player_id=int(ctx.author.id),
            players={
                int(ctx.author.id): str(ctx.author.name),
                int(opponent.id): str(opponent.name),
            },
            player_avatars=avatar_data,
        )
        # The shared Blackjack model computes each player's payout.  Wager
        # reservations are kept by the controller because its compatibility
        # model intentionally stores a single legacy wager ID.
        game.stake_by_player = stake_by_player
        view = BlackjackView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_blackjack_duel,
        )
        self._blackjack_duels[key] = game
        self._blackjack_duel_wagers[key] = wager_ids
        try:
            # Wagered DuelBidView already uses Components V2, while the
            # no-bid acceptance view uses classic components.  Discord
            # rejects legacy fields on a V2 update, so tailor the payload to
            # the source message just like the other duel controllers do.
            components_v2 = bool(
                interaction.message is not None
                and getattr(
                    getattr(interaction.message, "flags", None),
                    "components_v2",
                    False,
                )
            )
            if components_v2:
                view.message = await interaction.edit_original_response(
                    view=view,
                    attachments=view.card_files(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                view.message = await interaction.edit_original_response(
                    content=None,
                    embed=None,
                    attachments=view.card_files(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            # The accepting player can receive their initial hand privately;
            # everyone can request their own hand later through View cards.
            if interaction.user.id in game.participant_ids:
                await view.send_private_cards(interaction)
            # Cards are kept private in Discord through the ephemeral panel;
            # no card information is sent by DM.  The challenger can open
            # their own panel with the public View cards button.
        except Exception:
            self._blackjack_duels.pop(key, None)
            self._blackjack_duel_wagers.pop(key, None)
            view.stop()
            for user_id, ids in wager_ids.items():
                for wager_id in ids:
                    try:
                        await self.bot.currency.settle_wager(
                            wager_id,
                            stake_by_player.get(user_id, 0),
                            track_stats=False,
                        )
                    except Exception:
                        self.bot.logger.exception(
                            "Failed to refund Blackjack duel after message setup failed"
                        )
            raise

    async def _double_blackjack_duel(self, game: BlackjackGame, user_id: int) -> bool:
        # Player-versus-player Blackjack is winner-takes-all and never
        # permits the house-style Double action.  Keep this guard in the
        # controller as well as the model/view so a stale component or direct
        # callback cannot create an extra wager.
        if game.house_player_id is not None:
            return False
        key = frozenset(game.participant_ids)
        player_stake = game.stake_by_player.get(int(user_id), game.stake)
        if not player_stake:
            return True
        if not self._currency_tracking_enabled(int(user_id)):
            return False
        try:
            wager = await self.bot.currency.open_wager(
                int(user_id), player_stake, source="pvp_blackjack"
            )
        except (InvalidAmount, InsufficientFunds):
            return False
        self._blackjack_duel_wagers.setdefault(key, {}).setdefault(
            int(user_id), []
        ).append(wager.id)
        return True

    async def _finish_blackjack_duel(self, game: BlackjackGame) -> None:
        key = frozenset(game.participant_ids)
        if self._blackjack_duels.get(key) is not game:
            return
        self._blackjack_duels.pop(key, None)
        wagers = self._blackjack_duel_wagers.pop(key, {})
        for user_id in game.participant_ids:
            payout = game.player_payouts.get(user_id, 0)
            ids = wagers.get(user_id, [])
            if not ids:
                continue
            base, remainder = divmod(payout, len(ids))
            try:
                for index, wager_id in enumerate(ids):
                    await self.bot.currency.settle_wager(
                        wager_id,
                        base + (remainder if index == 0 else 0),
                    )
                game.player_payouts[user_id] = payout
            except Exception:
                self.bot.logger.exception("Failed to settle Blackjack duel wager")
        if game.view is not None:
            game.view.refresh()
            game.view.stop()
            # Keep any already-open private card panels in sync with the
            # settled public round and disable their gameplay controls.
            try:
                await game.view._update_private_views()
            except (discord.HTTPException, discord.NotFound):
                pass
            for private_view in game.view.private_views.values():
                private_view.stop()
            if game.view.message is not None:
                try:
                    await game.view.message.edit(
                        view=game.view,
                        attachments=game.view.card_files(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

    @commands.command(name="crash")
    async def crash(
        self,
        ctx: Context,
        *,
        amount: str = commands.param(
            default="100",
            description="Coin wager, at least 10 Coins (defaults to 100).",
        ),
    ) -> None:
        """Ride a random multiplier and cash out before it crashes."""
        await self._start_crash(ctx, amount)

    @game.command(name="crash")
    @app_commands.describe(amount=COIN_AMOUNT_DESCRIPTION)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_crash(
        self,
        ctx: Context,
        amount: str = commands.param(
            default="100",
            description=f"{COIN_AMOUNT_DESCRIPTION[:-1]} Defaults to 100.",
        ),
    ) -> None:
        """Ride a random multiplier and cash out before it crashes."""
        await self._start_crash(ctx, amount)

    @commands.command(
        name="lightsout",
        aliases=("lights-out", "lights"),
    )
    async def lightsout(self, ctx: Context) -> None:
        """Turn off every light in a randomized five by five puzzle."""
        await self._start_lightsout(ctx)

    @commands.group(
        name="higher-or-lower",
        aliases=(
            "hol",
            "higher",
            "lower",
            "higherorlower",
            "highorlow",
            "higherlower",
            "highlow",
        ),
        invoke_without_command=True,
    )
    async def higher_or_lower(self, ctx: Context, *, amount: str | None = None) -> None:
        """Guess whether each new card is higher or lower, optionally wagering Coins."""
        await self._start_higher_or_lower(ctx, amount)

    @higher_or_lower.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def higher_or_lower_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Higher or Lower streak statistics."""
        await self._send_streak_game_stats(
            ctx, "higher_or_lower", "Higher or Lower", user
        )

    @game.command(name="higher-or-lower")
    @app_commands.describe(amount=OPTIONAL_COIN_AMOUNT_DESCRIPTION)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_higher_or_lower(
        self,
        ctx: Context,
        amount: str | None = commands.param(
            default=None,
            description=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Guess whether each new card is higher or lower, optionally wagering Coins."""
        await self._start_higher_or_lower(ctx, amount)

    @commands.group(
        name="heads-or-tails",
        aliases=("headsortails", "headortail", "coinflip", "cf"),
        invoke_without_command=True,
        extras={"usage": "[user] [bid]"},
    )
    async def heads_or_tails(self, ctx: Context, *arguments: str) -> None:
        """Guess an endless series of coin flips, optionally wagering Coins."""
        user, amount = await parse_duel_arguments(ctx, arguments)
        bot_id = (
            ctx.bot.user.id
            if ctx.bot.user is not None
            else int(ctx.bot.config["ids"]["bot_id"])
        )
        if user is None:
            await self._start_heads_or_tails(ctx, amount)
            return
        if user.id == bot_id:
            if amount is not None:
                await ctx.send(
                    "Fishie games use the normal daily reward and cannot have a Coin bid.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            await self._start_heads_or_tails(ctx, None)
            return
        if user.id == ctx.author.id:
            await ctx.send("You cannot play Heads or Tails against yourself.")
            return
        if user.bot:
            await ctx.send("You cannot challenge another bot to Heads or Tails.")
            return
        if not account_is_old_enough(ctx.author):
            await ctx.send(account_age_message(ctx.author))
            return
        if not account_is_old_enough(user):
            await ctx.send(account_age_message(user))
            return
        if amount is not None and amount < DUEL_MIN_BID:
            await ctx.send(
                f"Player bids must be at least **{DUEL_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._challenge_pvp_choice(
            ctx,
            user,
            game_key="heads_tails",
            game_name="Heads or Tails",
            choices=("heads", "tails"),
            amount=amount,
        )

    @heads_or_tails.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def heads_or_tails_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Heads or Tails streak statistics."""
        await self._send_streak_game_stats(
            ctx, "heads_or_tails", "Heads or Tails", user
        )

    @game.command(name="heads-or-tails")
    @app_commands.describe(
        user="The user to challenge. Omit this to play Fishie.",
        amount=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_heads_or_tails(
        self,
        ctx: Context,
        user: discord.User | None = commands.param(
            default=None,
            description="The user to challenge. Omit this to play Fishie.",
        ),
        amount: str | None = commands.param(
            default=None,
            description=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Guess an endless series of coin flips, optionally wagering Coins."""
        arguments: list[object] = []
        if user is not None:
            arguments.append(user)
        if amount is not None:
            arguments.append(amount)
        await cast(Any, self.heads_or_tails)(ctx, *arguments)

    @commands.group(name="wordle", aliases=("wdl",), invoke_without_command=True)
    async def wordle(self, ctx: Context) -> None:
        """Play a solo six-guess Wordle game."""
        await self._start_wordle(ctx)

    @wordle.command(name="settings")
    async def wordle_settings(self, ctx: Context) -> None:
        """Configure your Wordle hard-mode and colourblind preferences."""
        hard, colourblind = await get_wordle_settings(self.bot.pool, ctx.author.id)
        view = WordleSettingsView(
            ctx.author.id,
            hard,
            colourblind,
            self._save_wordle_settings,
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @game.command(name="wordle")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_wordle(self, ctx: Context) -> None:
        """Play a solo six-guess Wordle game."""
        await self._start_wordle(ctx)

    @game.command(
        name="wordbomb",
        aliases=("wb", "word-bomb"),
        description="Open a Word Bomb lobby and invite players.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_wordbomb(self, ctx: Context) -> None:
        """Start a Word Bomb lobby."""
        await self._start_wordbomb(ctx)

    @game.command(
        name="last-letter",
        aliases=("lastl", "lastletter"),
        description="Open a LastLetter lobby and invite players.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_last_letter(self, ctx: Context) -> None:
        """Start a LastLetter lobby."""
        await self._start_lastletter(ctx)

    @game.command(name="memory", aliases=("matching",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_memory(self, ctx: Context) -> None:
        """Play a solo four by four memory matching game."""
        await self._start_memory(ctx)

    @commands.command(name="memory", aliases=("matching",))
    async def memory(self, ctx: Context) -> None:
        """Play a solo four by four memory matching game."""
        await self._start_memory(ctx)

    @game.command(name="rock-paper-scissors")
    @app_commands.describe(
        user="The user to challenge. Omit this to play Fishie.",
        amount=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_rock_paper_scissors(
        self,
        ctx: Context,
        user: discord.User | None = commands.param(
            default=None,
            description="The user to challenge. Omit this to play Fishie.",
        ),
        amount: str | None = commands.param(
            default=None,
            description=OPTIONAL_COIN_AMOUNT_DESCRIPTION,
        ),
    ) -> None:
        """Play rock paper scissors, optionally wagering Coins on a win streak."""
        arguments: list[object] = []
        if user is not None:
            arguments.append(user)
        if amount is not None:
            arguments.append(amount)
        await cast(Any, self.RPSCommand)(ctx, *arguments)

    @commands.group(
        name="rock-paper-scissors",
        aliases=("rockpaperscissors", "rps"),
        invoke_without_command=True,
        extras={"usage": "[user] [bid]"},
    )
    async def RPSCommand(self, ctx: Context, *arguments: str) -> None:
        """Play rock paper scissors, optionally wagering Coins on a win streak."""
        user, amount = await parse_duel_arguments(ctx, arguments)
        bot_id = (
            ctx.bot.user.id
            if ctx.bot.user is not None
            else int(ctx.bot.config["ids"]["bot_id"])
        )
        if user is None:
            await self._start_rock_paper_scissors(ctx, amount)
            return
        if user.id == bot_id:
            if amount is not None:
                await ctx.send(
                    "Fishie games use the normal daily reward and cannot have a Coin bid.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            await self._start_rock_paper_scissors(ctx, None)
            return
        if user.id == ctx.author.id:
            await ctx.send("You cannot play Rock Paper Scissors against yourself.")
            return
        if user.bot:
            await ctx.send("You cannot challenge another bot to Rock Paper Scissors.")
            return
        if not account_is_old_enough(ctx.author):
            await ctx.send(account_age_message(ctx.author))
            return
        if not account_is_old_enough(user):
            await ctx.send(account_age_message(user))
            return
        if amount is not None and amount < DUEL_MIN_BID:
            await ctx.send(
                f"Player bids must be at least **{DUEL_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._challenge_pvp_choice(
            ctx,
            user,
            game_key="rock_paper_scissors",
            game_name="Rock Paper Scissors",
            choices=("rock", "paper", "scissors"),
            amount=amount,
        )

    @RPSCommand.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def rock_paper_scissors_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show wagered Rock Paper Scissors streak statistics."""
        await self._send_streak_game_stats(
            ctx, "rock_paper_scissors", "Rock Paper Scissors", user
        )

    @commands.command(name="monark")
    @commands.cooldown(1, 5)
    async def monark(self, ctx: Context):
        """monark said this"""

        await ctx.send(
            file=discord.File(
                FILES_ROOT / "monark" / f"monark{random.randint(1, 3)}.png",
                "monark.png",
            )
        )

    @commands.command(name="hattori")
    @commands.cooldown(1, 3)
    async def hattori(self, ctx: Context):
        """hattori"""
        name = "hattori2" if random.randint(0, 15) == 6 else "hattori"
        await ctx.send(
            file=discord.File(FILES_ROOT / "images" / f"{name}.png", "hattori.png")
        )

    @commands.command(name="merica", aliases=("cm",))
    @commands.cooldown(1, 5)
    async def merica(self, ctx: Context, *, text: str):
        """we love america!!!"""

        await ctx.send(
            re.sub(" ", " \U0001f1fa\U0001f1f8 ", text)[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="invite", aliases=("join",))
    async def invite(self, ctx: Context):
        """Sends a link to add me to a server."""

        await ctx.send(self.invite_url)

    @commands.command(
        name="firstmessage",
        aliases=("firstmsg", "fmsg"),
    )
    async def firstmessage(self, ctx: Context) -> None:
        """Link to the first message sent in this channel and reply to it."""

        history = getattr(ctx.channel, "history", None)
        if history is None:
            await ctx.send("I cannot search message history in this channel.")
            return

        first_message: discord.Message | None = None
        try:
            async for message in history(limit=1, oldest_first=True):
                first_message = message
                break
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send("I cannot read message history in this channel.")
            return

        if first_message is None:
            await ctx.send("I could not find a message in this channel.")
            return

        await ctx.send_new(
            f"[Jump to the first message]({first_message.jump_url})",
            reference=first_message,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(
        name="tictactoe",
        aliases=("ttt", "tic-tic-toe"),
        invoke_without_command=True,
        extras={"usage": "[user] [bid]"},
    )
    async def tictactoe(self, ctx: Context, *arguments: str) -> None:
        """Play Tic-Tac-Toe against another user or Fishie."""

        checker = getattr(self, "_require_currency_tracking", None)
        if callable(checker) and not await cast(Any, checker)(ctx):
            return

        if len(arguments) == 1 and isinstance(arguments[0], discord.abc.User):
            # The slash-only ``/game tic-tac-toe`` child delegates here with
            # an already-converted User object.
            user, amount = arguments[0], None  # type: ignore[assignment]
        else:
            user, amount = await parse_duel_arguments(ctx, arguments)
        bot_id = (
            ctx.bot.user.id
            if ctx.bot.user is not None
            else int(ctx.bot.config["ids"]["bot_id"])
        )
        if user is None or user.id == bot_id:
            if amount is not None:
                await ctx.send(
                    "Fishie games use the normal daily reward and cannot have a Coin bid.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            view = self.tictactoe_mode_view(
                ctx,
                prompt=(
                    "Mention a user to play against them, or choose a difficulty "
                    "below to play against Fishie."
                    if user is None
                    else "Choose a difficulty below to play against Fishie."
                ),
            )
            view.message = await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if user.id == ctx.author.id:
            await ctx.send("You cannot play Tic-Tac-Toe against yourself.")
            return
        if user.bot:
            await ctx.send("You cannot challenge another bot to Tic-Tac-Toe.")
            return
        if not account_is_old_enough(ctx.author):
            await ctx.send(account_age_message(ctx.author))
            return
        if not account_is_old_enough(user):
            await ctx.send(account_age_message(user))
            return
        if amount is not None and amount < DUEL_MIN_BID:
            await ctx.send(
                f"Player bids must be at least **{DUEL_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        view = self.tictactoe_challenge_view(ctx, user, challenger_bid=amount)
        view.message = await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tictactoe.command(name="stats", aliases=("leaderboard", "lb"))
    async def tictactoe_stats(self, ctx: Context) -> None:
        """Show global Tic-Tac-Toe win and loss leaderboards."""

        await self._tictactoe_controller.send_stats(ctx)

    @commands.command(name="bid")
    async def bid(self, ctx: Context, *, amount: str) -> None:
        """Change your bid in a pending player-vs-player game."""

        view = DuelBidView.active_for(ctx.channel.id, ctx.author.id)
        if view is None:
            await ctx.send(
                "You do not have a pending player duel in this channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            parsed_amount = parse_coin_amount(amount)
        except CoinAmountError:
            await ctx.send(
                f"Bids must be at least {DUEL_MIN_BID:,} Coins.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        resolved_amount: int
        if parsed_amount == EVERYTHING_AMOUNT:
            wallet = await self.bot.currency.get_wallet(ctx.author.id)
            resolved_amount = int(wallet.balance)
            if resolved_amount < DUEL_MIN_BID:
                await ctx.send(
                    f"You need at least {DUEL_MIN_BID:,} Coins to bid everything.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if (
                await ctx.prompt(
                    "Are you sure you want to bid everything?",
                    confirm_label="Yes",
                    cancel_label="No",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                is None
            ):
                return
        else:
            resolved_amount = int(parsed_amount)
        if resolved_amount < DUEL_MIN_BID:
            await ctx.send(
                f"Bids must be at least {DUEL_MIN_BID:,} Coins.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await view.set_bid_from_command(ctx.author.id, resolved_amount):
            await ctx.send(
                "Bids can no longer be changed for that duel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send(
            f"Your duel bid is now **{resolved_amount:,} Coins**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _start_pvp_choice_game(
        self,
        ctx: Context,
        opponent: discord.abc.User,
        *,
        game_key: str,
        game_name: str,
        choices: tuple[str, ...],
        stakes: tuple[int, int] | None,
        interaction: discord.Interaction,
    ) -> None:
        """Escrow each player's optional wager and start a choice game."""

        key = frozenset((int(ctx.author.id), int(opponent.id)))
        if key in self._pvp_duels:
            message = "One of those users already has a player game in progress."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return

        # Escrow and board construction can involve several database calls.
        # Acknowledge the button interaction first so a slow wallet cannot
        # make Discord report an interaction timeout.
        if not interaction.response.is_done():
            await interaction.response.defer()
        wager_ids: dict[int, int] = {}
        stake_by_player: dict[int, int] = {}
        if stakes is not None:
            if len(stakes) != 2 or any(int(stake) < DUEL_MIN_BID for stake in stakes):
                await interaction.followup.send(
                    f"Each player bid must be at least {DUEL_MIN_BID:,} Coins.",
                    ephemeral=True,
                )
                return
            stake_by_player = {
                int(ctx.author.id): int(stakes[0]),
                int(opponent.id): int(stakes[1]),
            }
            if not all(
                self._currency_tracking_enabled(user_id)
                for user_id in (ctx.author.id, opponent.id)
            ):
                await interaction.followup.send(
                    "Both players must have currency tracking enabled to place a bid.",
                    ephemeral=True,
                )
                return
            try:
                for user_id, player_stake in stake_by_player.items():
                    wager = await self.bot.currency.open_wager(
                        user_id, player_stake, source=f"pvp_{game_key}"
                    )
                    wager_ids[user_id] = wager.id
            except Exception as error:
                for user_id, wager_id in wager_ids.items():
                    try:
                        await self.bot.currency.settle_wager(
                            wager_id,
                            stake_by_player[user_id],
                            track_stats=False,
                        )
                    except Exception:
                        self.bot.logger.exception("Failed to refund player duel wager")
                await interaction.followup.send(
                    f"I couldn't reserve both bids: {error}", ephemeral=True
                )
                return

        async def resolve(
            resolve_interaction: discord.Interaction, selections: dict[int, str]
        ) -> None:
            first = selections.get(ctx.author.id)
            second = selections.get(opponent.id)
            winner_id: int | None = None
            if game_key == "heads_tails":
                if first != second:
                    # When the players pick opposite sides, flip a fair coin
                    # to decide which side wins.  Matching picks are a draw.
                    winner_id = random.choice((ctx.author.id, opponent.id))
            else:
                beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
                if first != second:
                    winner_id = (
                        ctx.author.id
                        if beats.get(first or "") == second
                        else opponent.id
                    )
            if wager_ids:
                pool = sum(stake_by_player.values())
                try:
                    if winner_id is None:
                        for user_id, wager_id in wager_ids.items():
                            await self.bot.currency.settle_wager(
                                wager_id,
                                stake_by_player[user_id],
                                track_stats=False,
                            )
                    else:
                        for user_id, wager_id in wager_ids.items():
                            await self.bot.currency.settle_wager(
                                wager_id, pool if user_id == winner_id else 0
                            )
                except Exception:
                    self.bot.logger.exception("Failed to settle player duel wager")
            self._pvp_duels.pop(key, None)
            if winner_id is None:
                result = (
                    "Draw · both bids were returned." if stake_by_player else "Draw."
                )
            else:
                winner = ctx.author if winner_id == ctx.author.id else opponent
                result = f"**{discord.utils.escape_markdown(winner.name)}** wins!"
                if stake_by_player:
                    result += f" They receive **{pool:,} Coins**."
            # Keep each selection private until both players have locked in;
            # once the round resolves, reveal both choices alongside the
            # outcome for a clear audit of the result.
            first_label = discord.utils.escape_markdown((first or "unknown").title())
            second_label = discord.utils.escape_markdown((second or "unknown").title())
            choice_view.status.content = (
                f"## {game_name}\n"
                f"**{discord.utils.escape_markdown(ctx.author.name)}:** {first_label}\n"
                f"**{discord.utils.escape_markdown(opponent.name)}:** {second_label}\n"
                f"{result}"
            )
            if resolve_interaction.message is not None:
                await resolve_interaction.message.edit(
                    view=choice_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        async def expire() -> None:
            """Return reserved bids when a choice duel times out."""

            self._pvp_duels.pop(key, None)
            for user_id, wager_id in wager_ids.items():
                try:
                    await self.bot.currency.settle_wager(
                        wager_id,
                        stake_by_player[user_id],
                        track_stats=False,
                    )
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund timed-out player duel wager"
                    )

        choice_view = DuelChoiceView(
            ctx.author,
            opponent,
            game_name=game_name,
            choices=choices,
            on_resolve=resolve,
            accent_color=self.bot.embedcolor,
            on_expire=expire,
        )
        self._pvp_duels[key] = choice_view
        try:
            # A player-choice duel may start from either the legacy
            # ``DuelChallengeView`` or the Components V2 bid view.  Discord
            # requires clearing content/embed/attachments when introducing a
            # LayoutView to a legacy message, but rejects those fields once a
            # message already has IS_COMPONENTS_V2.  Build the edit payload
            # according to the source message so both paths are valid.
            components_v2 = bool(
                interaction.message is not None
                and getattr(
                    getattr(interaction.message, "flags", None),
                    "components_v2",
                    False,
                )
            )
            if components_v2:
                # Do not include content/embeds/attachments in a V2 update.
                choice_view.message = await interaction.edit_original_response(
                    view=choice_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                choice_view.message = await interaction.edit_original_response(
                    content=None,
                    embed=None,
                    attachments=[],
                    view=choice_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:
            self._pvp_duels.pop(key, None)
            choice_view.stop()
            for user_id, wager_id in wager_ids.items():
                try:
                    await self.bot.currency.settle_wager(
                        wager_id,
                        stake_by_player.get(user_id, 0),
                        track_stats=False,
                    )
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund player duel after message setup failed"
                    )
            raise

    async def _challenge_pvp_choice(
        self,
        ctx: Context,
        opponent: discord.abc.User,
        *,
        game_key: str,
        game_name: str,
        choices: tuple[str, ...],
        amount: int | None,
    ) -> None:
        async def start(
            interaction: discord.Interaction, stakes: tuple[int, int] | None
        ) -> None:
            await self._start_pvp_choice_game(
                ctx,
                opponent,
                game_key=game_key,
                game_name=game_name,
                choices=choices,
                stakes=stakes,
                interaction=interaction,
            )

        if amount is None:

            async def accept(interaction: discord.Interaction) -> None:
                await start(interaction, None)

            view = DuelChallengeView(
                ctx,
                ctx.author,
                opponent,
                game_name=game_name,
                on_accept=accept,
            )
        else:

            async def ready(
                interaction: discord.Interaction, stakes: tuple[int, int]
            ) -> None:
                await start(interaction, stakes)

            view = DuelBidView(
                ctx,
                ctx.author,
                opponent,
                game_name=game_name,
                challenger_bid=amount,
                on_ready=ready,
            )
        if isinstance(view, DuelBidView):
            # LayoutView uses Components V2; its TextDisplay already contains
            # the prompt, so Discord rejects a separate message ``content``.
            view.message = await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            view.message = await ctx.send(
                (
                    f"{opponent.mention}, **{discord.utils.escape_markdown(opponent.name)}** "
                    f"was challenged to {game_name} by "
                    f"**{discord.utils.escape_markdown(ctx.author.name)}**."
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @commands.group(
        name="connect4",
        aliases=("connect-four", "c4", "connect"),
        invoke_without_command=True,
        extras={"usage": "[user] [bid]"},
    )
    async def connectfour(self, ctx: Context, *arguments: str) -> None:
        """Play Connect Four against another user or Fishie."""

        checker = getattr(self, "_require_currency_tracking", None)
        if callable(checker) and not await cast(Any, checker)(ctx):
            return

        if len(arguments) == 1 and isinstance(arguments[0], discord.abc.User):
            user, amount = arguments[0], None  # type: ignore[assignment]
        else:
            user, amount = await parse_duel_arguments(ctx, arguments)
        bot_id = (
            ctx.bot.user.id
            if ctx.bot.user is not None
            else int(ctx.bot.config["ids"]["bot_id"])
        )
        # A missing target (or any bot target) uses the house-game flow.  For
        # another bot we still keep Fishie as the internal opponent for stats
        # and rewards, while Connect Four displays that bot's name for a more
        # natural challenge UI.
        if user is None or user.bot:
            if amount is not None:
                await ctx.send(
                    "Fishie games use the normal daily reward and cannot have a Coin bid.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            view = ConnectFourSetupView(
                self._connectfour_controller,
                ctx,
                None if user is None or user.id == bot_id else user,
            )
            view.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        if user.id == ctx.author.id:
            await ctx.send("You cannot play Connect Four against yourself.")
            return
        if not account_is_old_enough(ctx.author):
            await ctx.send(account_age_message(ctx.author))
            return
        if not account_is_old_enough(user):
            await ctx.send(account_age_message(user))
            return
        if amount is not None and amount < DUEL_MIN_BID:
            await ctx.send(
                f"Player bids must be at least **{DUEL_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = ConnectFourChallengeView(
            self._connectfour_controller,
            ctx,
            user,
            challenger_bid=amount,
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @connectfour.command(name="stats", aliases=("leaderboard", "lb"))
    async def connectfour_stats(self, ctx: Context) -> None:
        """Show global Connect Four win and loss leaderboards."""

        await self._connectfour_controller.send_stats(ctx)

    @commands.command(name="dice", aliases=("roll",))
    async def dice(
        self,
        ctx: Context,
        sides: int = commands.param(
            default=6, description="Number of sides on each die (defaults to 6)."
        ),
        rolls: int = commands.param(
            default=1, description="Number of dice to roll, up to 10."
        ),
    ) -> None:
        """Roll one or more dice, such as `fish dice 20 2`."""
        await self._roll_dice(ctx, sides, rolls)

    async def _roll_dice(self, ctx: Context, sides: int, rolls: int) -> None:
        if sides < 2 or sides > 1_000_000:
            raise commands.BadArgument("Dice must have between 2 and 1,000,000 sides.")
        if rolls < 1 or rolls > 10:
            raise commands.BadArgument("You can roll between 1 and 10 dice at once.")
        values = [random.SystemRandom().randint(1, sides) for _ in range(rolls)]
        result = ", ".join(map(str, values))
        total = sum(values)
        await ctx.send(
            f"🎲 **{rolls}d{sides}:** {result}\n**Total:** {total:,}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(
        name="color",
        aliases=("colour", "colormemorize", "colourmemorize"),
        invoke_without_command=True,
    )
    async def color(
        self,
        ctx: Context,
        difficulty: str = commands.param(
            default="normal",
            description="Difficulty: easy, normal, hard, extreme, or impossible.",
        ),
    ) -> None:
        """Memorize a flashing sequence of colors."""
        await self._start_color_memorize(ctx, difficulty)

    @color.command(name="stats")
    async def color_stats(self, ctx: Context) -> None:
        """Show Color Memorize wins and failed attempts by difficulty."""
        rows = await ctx.bot.pool.fetch(
            "SELECT difficulty, user_id, wins, fails FROM minigame_stats "
            "WHERE game = 'color_memorize' ORDER BY difficulty, wins DESC, fails ASC"
        )
        rows = [
            row
            for row in rows
            if self.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ]
        embed = discord.Embed(
            title="Color Memorize stats",
            color=ctx.bot.embedcolor,
        )
        grouped: dict[str, list] = {
            difficulty: [] for difficulty in COLOR_MEMORIZE_DIFFICULTIES
        }
        for row in rows:
            difficulty = str(row["difficulty"])
            if difficulty in grouped and len(grouped[difficulty]) < 5:
                grouped[difficulty].append(row)
        for difficulty in COLOR_MEMORIZE_DIFFICULTIES:
            values = grouped[difficulty]
            if not values:
                text = "No games recorded."
            else:
                lines = []
                for index, row in enumerate(values, start=1):
                    user = await get_or_fetch_user(ctx.bot, int(row["user_id"]))
                    name = user.name if user else str(row["user_id"])
                    lines.append(
                        f"**{index}. {discord.utils.escape_markdown(name)}** · "
                        f"{int(row['wins']):,} wins · {int(row['fails']):,} fails"
                    )
                text = "\n".join(lines)
            embed.add_field(name=difficulty.title(), value=text, inline=False)
        await ctx.send(embed=embed)

    async def _start_2048(self, ctx: Context) -> None:
        if ctx.author.id in self._2048_games:
            await ctx.send(
                "You already have an active 2048 game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        game = Game2048(
            owner_id=ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
        )
        view = Game2048View(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_2048,
        )
        self._2048_games[ctx.author.id] = game
        try:
            view.message = await ctx.send(
                view=view,
                file=view.render_file(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._2048_games.pop(ctx.author.id, None)
            view.stop()
            raise

    async def _finish_2048(self, game: Game2048, timed_out: bool) -> None:
        if self._2048_games.get(game.owner_id) is not game:
            return
        self._2048_games.pop(game.owner_id, None)
        if not self.bot.db_cache.user_game_tracking_enabled(game.owner_id):
            return
        await self.bot.pool.execute(
            """
            INSERT INTO game_2048_games
                (user_id, guild_id, channel_id, score, highest_tile,
                 move_count, move_history, timed_out, gave_up,
                 duration_seconds, started_at, finished_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, now())
            """,
            game.owner_id,
            game.guild_id,
            game.channel_id,
            game.score,
            highest_tile(game.board),
            game.move_count,
            json.dumps(game.move_history, separators=(",", ":")),
            game.timed_out,
            game.gave_up,
            round(game.duration_seconds, 3),
            game.started_at,
        )
        await self.bot.pool.execute(
            """
            INSERT INTO game_2048_stats
                (user_id, high_score, total_playtime_seconds, games_completed)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                high_score = GREATEST(game_2048_stats.high_score, EXCLUDED.high_score),
                total_playtime_seconds = game_2048_stats.total_playtime_seconds
                    + EXCLUDED.total_playtime_seconds,
                games_completed = game_2048_stats.games_completed
                    + EXCLUDED.games_completed,
                updated_at = now()
            """,
            game.owner_id,
            game.score,
            0 if timed_out else int(game.duration_seconds),
            0 if timed_out else 1,
        )

    async def _start_lightsout(self, ctx: Context) -> None:
        checker = getattr(self, "_require_currency_tracking", None)
        if callable(checker) and not await cast(Any, checker)(ctx):
            return
        if ctx.author.id in self._lightsout_games:
            await ctx.send(
                "You already have an active Lights Out game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        game = LightsOutGame.new(
            ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild is not None else None,
            channel_id=ctx.channel.id,
        )
        view = LightsOutView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_lightsout,
        )
        self._lightsout_games[ctx.author.id] = game
        try:
            view.message = await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._lightsout_games.pop(ctx.author.id, None)
            view.stop()
            raise

    async def _finish_lightsout(
        self,
        game: LightsOutGame,
        timed_out: bool,
    ) -> None:
        if self._lightsout_games.get(game.user_id) is not game:
            return
        self._lightsout_games.pop(game.user_id, None)
        if timed_out or not game.finished:
            return
        try:
            # Allow multiple completions to award up to 5,000 Coins per day;
            # the individual completion payout still depends on solve time.
            game.reward_coins = await award_daily_capped_coins(
                self.bot.pool,
                game.user_id,
                completion_payout(game.duration_seconds),
                DAILY_PAYOUT_CAP,
                "lightsout",
            )
        except Exception:
            self.bot.logger.exception("Failed to award Lights Out Coins")
            game.reward_coins = 0
        if game.view is not None:
            # The interaction initially edits the board before this callback
            # runs.  Refresh once more so the completed view includes the
            # actual (daily-cap-adjusted) reward.
            game.view.refresh()
            if game.view.message is not None:
                try:
                    await game.view.message.edit(
                        view=game.view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        if not self.bot.db_cache.user_game_tracking_enabled(game.user_id):
            return
        await self.bot.pool.execute(
            """
            INSERT INTO lightsout_games
                (user_id, guild_id, channel_id, move_count,
                 duration_seconds, started_at, finished_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            """,
            game.user_id,
            game.guild_id,
            game.channel_id,
            game.move_count,
            float(game.duration_seconds),
            game.started_at,
        )

    async def _start_mines(self, ctx: Context, amount: object, bombs: int) -> None:
        """Reserve a wager and start an author-only Mines board."""

        if not await self._require_currency_tracking(ctx):
            return
        user_id = ctx.author.id
        if user_id in self._mines_games:
            await ctx.send(
                "You already have an active Mines game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        amount_value, stop = await self._resolve_coin_amount(ctx, amount)
        if stop or amount_value is None:
            return
        try:
            amount = int(amount_value)
            bombs = int(bombs)
        except (TypeError, ValueError):
            await ctx.send(
                "Mines requires a valid Coin wager and mine count.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if amount < GAME_MIN_BID:
            await ctx.send(
                f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not MIN_BOMBS <= bombs <= MAX_BOMBS:
            await ctx.send(
                f"Mines must be between **{MIN_BOMBS}** and **{MAX_BOMBS}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            wager = await self.bot.currency.open_wager(
                user_id,
                amount,
                source="mines",
            )
        except InvalidAmount:
            await ctx.send(
                f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except InsufficientFunds as error:
            await ctx.send(
                f"You only have **{error.balance:,} Coins**, so you can't wager "
                f"**{error.required:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        game = MinesGame.new(user_id, amount, wager.id, bombs)
        view = MinesView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_mines,
        )
        self._mines_games[user_id] = game
        try:
            view.message = await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._mines_games.pop(user_id, None)
            view.stop()
            try:
                await self.bot.currency.settle_wager(wager.id, wager.stake)
            except Exception:
                self.bot.logger.exception(
                    "Failed to refund a Mines wager after startup failed"
                )
            raise

    async def _finish_mines(self, game: MinesGame, outcome: str) -> None:
        """Settle a Mines wager exactly once and refresh its final display."""

        if self._mines_games.get(game.user_id) is not game or game.settled:
            return
        self._mines_games.pop(game.user_id, None)
        try:
            payout = game.payout if outcome in {"cashout", "cleared"} else 0
            settlement = await self.bot.currency.settle_wager(game.wager_id, payout)
            if settlement.wager.status == "cashed_out":
                game.payout = settlement.wager.payout
            elif payout:
                game.settlement_error = True
                game.payout = 0
            game.settled = True
        except Exception:
            game.settlement_error = True
            game.payout = 0
            self.bot.logger.exception("Failed to settle Mines wager")
        if game.view is not None:
            game.view.refresh()
            if game.view.message is not None:
                try:
                    await game.view.message.edit(
                        view=game.view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

    async def _start_crash(self, ctx: Context, amount: object) -> None:
        """Reserve a wager and start an author-only Crash game."""

        if not await self._require_currency_tracking(ctx):
            return
        user_id = int(ctx.author.id)
        active = self._crash_games.get(user_id)
        if active is not None and not active.finished:
            await ctx.send(
                "You already have an active Crash game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        amount_value, stop = await self._resolve_coin_amount(ctx, amount)
        if stop or amount_value is None:
            return
        try:
            amount = int(amount_value)
        except (TypeError, ValueError):
            await ctx.send(
                "Crash requires a valid Coin wager.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if amount < CRASH_MIN_BID:
            await ctx.send(
                f"Coin wagers must be at least **{CRASH_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            wager = await self.bot.currency.open_wager(
                user_id,
                amount,
                source="crash",
            )
        except InvalidAmount:
            await ctx.send(
                f"Coin wagers must be at least **{CRASH_MIN_BID:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except InsufficientFunds as error:
            await ctx.send(
                f"You only have **{error.balance:,} Coins**, so you can't wager "
                f"**{error.required:,} Coins**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        game = CrashGame(user_id, amount, wager.id)
        view = CrashView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_crash,
        )
        self._crash_games[user_id] = game
        try:
            game.message = await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.message = game.message
            game.task = asyncio.create_task(self._run_crash(game, view))
        except Exception:
            self._crash_games.pop(user_id, None)
            view.stop()
            try:
                await self.bot.currency.settle_wager(
                    wager.id, wager.stake, track_stats=False
                )
            except Exception:
                self.bot.logger.exception(
                    "Failed to refund a Crash wager after startup failed"
                )
            raise

    async def _run_crash(self, game: CrashGame, view: CrashView) -> None:
        """Advance the multiplier until cash-out, crash, or timeout."""

        try:
            while not game.finished:
                await asyncio.sleep(CRASH_TICK_SECONDS)
                async with game.lock:
                    if game.finished:
                        return
                    can_continue = game.advance()
                    view.refresh()
                    if game.message is not None:
                        await game.message.edit(
                            view=view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                if not can_continue:
                    view.stop()
                    await self._finish_crash(game, "crash")
                    return
        except asyncio.CancelledError:
            # Cog reload/shutdown must not strand the reserved stake.  A game
            # already settled by Cash out is harmless because settlement is
            # idempotent.
            if not game.settled:
                try:
                    await self.bot.currency.settle_wager(
                        game.wager_id, game.stake, track_stats=False
                    )
                except Exception:
                    self.bot.logger.exception("Failed to refund cancelled Crash wager")
            return
        except Exception:
            self.bot.logger.exception("Crash game loop failed")
            game.finished = True
            game.payout = 0
            try:
                await self._finish_crash(game, "error")
            except Exception:
                self.bot.logger.exception("Failed to settle failed Crash wager")

    async def _finish_crash(self, game: CrashGame, outcome: str) -> None:
        """Settle a Crash wager once and refresh the final display."""

        if self._crash_games.get(game.user_id) is not game or game.settled:
            return
        self._crash_games.pop(game.user_id, None)
        try:
            payout = game.payout if outcome == "cashout" else 0
            settlement = await self.bot.currency.settle_wager(game.wager_id, payout)
            if settlement.wager.status == "cashed_out":
                game.payout = settlement.wager.payout
            elif payout:
                game.settlement_error = True
                game.payout = 0
            game.settled = True
        except Exception:
            game.settlement_error = True
            game.payout = 0
            self.bot.logger.exception("Failed to settle Crash wager")
        if game.view is not None:
            game.view.refresh()
            if game.message is not None:
                try:
                    await game.message.edit(
                        view=game.view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

    async def _send_lightsout_stats(self, ctx: Context) -> None:
        """Show the five fastest tracked Lights Out completions."""

        rows = await self.bot.pool.fetch("""
            SELECT user_id, duration_seconds, move_count
            FROM (
                SELECT DISTINCT ON (user_id)
                    user_id, duration_seconds, move_count, finished_at
                FROM lightsout_games
                ORDER BY user_id, duration_seconds ASC, move_count ASC,
                         finished_at ASC
            ) personal_bests
            ORDER BY duration_seconds ASC, move_count ASC, user_id ASC
            """)
        rows = [
            row
            for row in rows
            if self.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ]
        # Apply the privacy filter before limiting the leaderboard so private
        # records do not hide otherwise eligible players from the top five.
        rows = rows[:5]
        lines = ["## Lights Out leaderboard"]
        if not rows:
            lines.append("No completed Lights Out puzzles have been recorded yet.")
        else:
            for index, row in enumerate(rows, start=1):
                listed_user = await get_or_fetch_user(self.bot, int(row["user_id"]))
                listed_name = getattr(listed_user, "name", None) or str(row["user_id"])
                safe_name = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(listed_name)
                )
                duration = float(row["duration_seconds"])
                lines.append(
                    f"**#{index} {safe_name}** · {duration:.2f}s · "
                    f"{int(row['move_count']):,} moves"
                )

        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def _record_streak_game(
        self, game_name: str, tracking_key: str, game: StreakGame
    ) -> None:
        if not self.bot.db_cache.user_game_tracking_enabled(game.user_id):
            return
        # A potential wager value is only a real payout after the player has
        # cashed out.  Recording it while a game is still active (or after a
        # loss/timeout) would make the leaderboard claim that an unclaimed
        # wager was earned.  The streak itself is still recorded on every
        # progress callback so unwagered games continue to build personal
        # bests.
        payout = 0
        if (
            isinstance(
                game, (HigherOrLowerGame, HeadsOrTailsGame, RockPaperScissorsGame)
            )
            and game.has_wager
            and game.cashed_out
        ):
            payout = max(0, int(game.potential_payout))
        try:
            await self.bot.pool.execute(
                """
                INSERT INTO streak_game_stats (
                    game, user_id, highest_streak, highest_streak_payout,
                    highest_payout, highest_payout_streak
                )
                VALUES ($1, $2, $3, $4, $4, $3)
                ON CONFLICT (game, user_id) DO UPDATE SET
                    highest_streak = GREATEST(
                        streak_game_stats.highest_streak,
                        EXCLUDED.highest_streak
                    ),
                    highest_streak_payout = CASE
                        WHEN EXCLUDED.highest_streak > streak_game_stats.highest_streak
                            THEN EXCLUDED.highest_streak_payout
                        WHEN EXCLUDED.highest_streak = streak_game_stats.highest_streak
                            AND EXCLUDED.highest_streak_payout
                                > streak_game_stats.highest_streak_payout
                            THEN EXCLUDED.highest_streak_payout
                        ELSE streak_game_stats.highest_streak_payout
                    END,
                    highest_payout = GREATEST(
                        streak_game_stats.highest_payout,
                        EXCLUDED.highest_payout
                    ),
                    highest_payout_streak = CASE
                        WHEN EXCLUDED.highest_payout
                            > streak_game_stats.highest_payout
                            THEN EXCLUDED.highest_payout_streak
                        WHEN EXCLUDED.highest_payout
                            = streak_game_stats.highest_payout
                            AND EXCLUDED.highest_payout_streak
                                > streak_game_stats.highest_payout_streak
                            THEN EXCLUDED.highest_payout_streak
                        ELSE streak_game_stats.highest_payout_streak
                    END,
                    updated_at = now()
                """,
                game_name,
                game.user_id,
                game.streak,
                payout,
            )
        except Exception:
            self.bot.logger.exception("Failed to record %s streak", game_name)

    async def _record_higher_or_lower_progress(self, game: StreakGame) -> None:
        await self._record_streak_game("higher_or_lower", "higher_lower", game)

    async def _record_heads_or_tails_progress(self, game: StreakGame) -> None:
        await self._record_streak_game("heads_or_tails", "heads_tails", game)

    async def _record_rock_paper_scissors_progress(self, game: StreakGame) -> None:
        await self._record_streak_game(
            "rock_paper_scissors", "rock_paper_scissors", game
        )

    async def _start_higher_or_lower(
        self, ctx: Context, amount: object | None = None
    ) -> None:
        resolver = getattr(self, "_resolve_coin_amount", None)
        if callable(resolver):
            amount, stop = await cast(Any, resolver)(ctx, amount)
            if stop:
                return
        elif amount is not None:
            try:
                parsed = parse_coin_amount(amount)
                if parsed == EVERYTHING_AMOUNT:
                    wallet = await self.bot.currency.get_wallet(ctx.author.id)
                    amount = int(wallet.balance)
                else:
                    amount = int(parsed)
            except (CoinAmountError, TypeError, ValueError):
                await ctx.send(
                    "Enter a valid Coin wager.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        checker = getattr(self, "_require_currency_tracking", None)
        if (
            amount is not None
            and callable(checker)
            and not await cast(Any, checker)(ctx)
        ):
            return
        if ctx.author.id in self._higher_or_lower_games:
            await ctx.send(
                "You already have an active Higher or Lower game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        wager = None
        if amount is not None:
            wager_amount = int(cast(Any, amount))
            if wager_amount < GAME_MIN_BID:
                await ctx.send(
                    f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                wager = await self.bot.currency.open_wager(
                    ctx.author.id,
                    wager_amount,
                    source="higher_lower",
                )
            except InvalidAmount:
                await ctx.send(
                    f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except InsufficientFunds as error:
                if error.balance == 0:
                    message = (
                        "Your wallet is empty, so you can't place that wager yet. "
                        "Use `daily` or `weekly` to collect Coins first."
                    )
                else:
                    message = (
                        f"You only have **{error.balance:,} Coins**, so you can't "
                        f"wager **{error.required:,} Coins**."
                    )
                await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
                return
        game = HigherOrLowerGame(
            ctx.author.id,
            wager_id=wager.id if wager is not None else None,
            stake=wager.stake if wager is not None else 0,
            payout_limit=MAX_WAGER_PAYOUT,
        )
        view = HigherOrLowerView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_higher_or_lower,
            on_progress=self._record_higher_or_lower_progress,
            on_cash_out=self._cash_out_higher_or_lower,
        )
        self._higher_or_lower_games[ctx.author.id] = game
        try:
            view.message = await ctx.send(
                view=view,
                file=view.card_file(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._higher_or_lower_games.pop(ctx.author.id, None)
            view.stop()
            if wager is not None:
                try:
                    await self.bot.currency.settle_wager(wager.id, wager.stake)
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund a Higher or Lower wager after startup failed"
                    )
            raise

    async def _finish_higher_or_lower(self, game: StreakGame, _timed_out: bool) -> None:
        active = self._higher_or_lower_games.get(game.user_id)
        if active is not game:
            return
        if isinstance(game, HigherOrLowerGame) and game.has_wager:
            if not game.cashed_out and game.wager_id is not None:
                try:
                    await self.bot.currency.settle_wager(game.wager_id, 0)
                except Exception:
                    self.bot.logger.exception(
                        "Failed to settle a lost Higher or Lower wager"
                    )
        self._higher_or_lower_games.pop(game.user_id, None)
        await self._record_higher_or_lower_progress(game)

    async def _cash_out_higher_or_lower(self, game: HigherOrLowerGame) -> bool:
        if game.wager_id is None:
            return False
        try:
            settlement = await self.bot.currency.settle_wager(
                game.wager_id, game.potential_payout
            )
        except BalanceOverflow:
            game.cash_out_error = (
                "Your wallet is too close to the Coin limit to hold this payout."
            )
            return False
        except Exception:
            self.bot.logger.exception("Failed to cash out a Higher or Lower wager")
            return False
        if settlement.wager.status != "cashed_out":
            game.cash_out_error = (
                "That wager has already ended and cannot be cashed out."
            )
            return False
        # Repeated/idempotent settlement returns the original stored payout.
        game.potential_payout = settlement.wager.payout
        return True

    async def _start_heads_or_tails(
        self, ctx: Context, amount: object | None = None
    ) -> None:
        resolver = getattr(self, "_resolve_coin_amount", None)
        if callable(resolver):
            amount, stop = await cast(Any, resolver)(ctx, amount)
            if stop:
                return
        elif amount is not None:
            try:
                parsed = parse_coin_amount(amount)
                if parsed == EVERYTHING_AMOUNT:
                    wallet = await self.bot.currency.get_wallet(ctx.author.id)
                    amount = int(wallet.balance)
                else:
                    amount = int(parsed)
            except (CoinAmountError, TypeError, ValueError):
                await ctx.send(
                    "Enter a valid Coin wager.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        checker = getattr(self, "_require_currency_tracking", None)
        if (
            amount is not None
            and callable(checker)
            and not await cast(Any, checker)(ctx)
        ):
            return
        if ctx.author.id in self._heads_or_tails_games:
            await ctx.send(
                "You already have an active Heads or Tails game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        wager = None
        if amount is not None:
            wager_amount = int(cast(Any, amount))
            if wager_amount < GAME_MIN_BID:
                await ctx.send(
                    f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                wager = await self.bot.currency.open_wager(
                    ctx.author.id,
                    wager_amount,
                    source="heads_tails",
                )
            except InvalidAmount:
                await ctx.send(
                    f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except InsufficientFunds as error:
                if error.balance == 0:
                    message = (
                        "Your wallet is empty, so you can't place that wager yet. "
                        "Use `daily` or `weekly` to collect Coins first."
                    )
                else:
                    message = (
                        f"You only have **{error.balance:,} Coins**, so you can't "
                        f"wager **{error.required:,} Coins**."
                    )
                await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
                return
        game = HeadsOrTailsGame(
            ctx.author.id,
            wager_id=wager.id if wager is not None else None,
            stake=wager.stake if wager is not None else 0,
            payout_limit=MAX_WAGER_PAYOUT,
        )
        view = HeadsOrTailsView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_heads_or_tails,
            on_progress=self._record_heads_or_tails_progress,
            on_restart=self._restart_heads_or_tails,
            on_cash_out=self._cash_out_heads_or_tails,
        )
        self._heads_or_tails_games[ctx.author.id] = game
        try:
            view.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            self._heads_or_tails_games.pop(ctx.author.id, None)
            view.stop()
            if wager is not None:
                try:
                    await self.bot.currency.settle_wager(wager.id, wager.stake)
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund a Heads or Tails wager after startup failed"
                    )
            raise

    async def _finish_heads_or_tails(self, game: StreakGame, _timed_out: bool) -> None:
        active = self._heads_or_tails_games.get(game.user_id)
        if active is not game:
            return
        if isinstance(game, HeadsOrTailsGame) and game.has_wager:
            if not game.cashed_out and game.wager_id is not None:
                try:
                    await self.bot.currency.settle_wager(game.wager_id, 0)
                except Exception:
                    self.bot.logger.exception(
                        "Failed to settle a lost Heads or Tails wager"
                    )
        self._heads_or_tails_games.pop(game.user_id, None)
        await self._record_heads_or_tails_progress(game)

    async def _cash_out_heads_or_tails(self, game: HeadsOrTailsGame) -> bool:
        if game.wager_id is None:
            return False
        try:
            settlement = await self.bot.currency.settle_wager(
                game.wager_id, game.potential_payout
            )
        except BalanceOverflow:
            game.cash_out_error = (
                "Your wallet is too close to the Coin limit to hold this payout."
            )
            return False
        except Exception:
            self.bot.logger.exception("Failed to cash out a Heads or Tails wager")
            return False
        if settlement.wager.status != "cashed_out":
            game.cash_out_error = (
                "That wager has already ended and cannot be cashed out."
            )
            return False
        game.potential_payout = settlement.wager.payout
        return True

    async def _restart_heads_or_tails(self, game: HeadsOrTailsGame) -> bool:
        """Re-register a finished coin-flip game before its view is restarted."""

        active = self._heads_or_tails_games.get(game.user_id)
        if active is not None and active is not game:
            return False
        self._heads_or_tails_games[game.user_id] = game
        return True

    async def _start_rock_paper_scissors(
        self, ctx: Context, amount: object | None = None
    ) -> None:
        resolver = getattr(self, "_resolve_coin_amount", None)
        if callable(resolver):
            amount, stop = await cast(Any, resolver)(ctx, amount)
            if stop:
                return
        elif amount is not None:
            try:
                parsed = parse_coin_amount(amount)
                if parsed == EVERYTHING_AMOUNT:
                    wallet = await self.bot.currency.get_wallet(ctx.author.id)
                    amount = int(wallet.balance)
                else:
                    amount = int(parsed)
            except (CoinAmountError, TypeError, ValueError):
                await ctx.send(
                    "Enter a valid Coin wager.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        checker = getattr(self, "_require_currency_tracking", None)
        if (
            amount is not None
            and callable(checker)
            and not await cast(Any, checker)(ctx)
        ):
            return
        if ctx.author.id in self._rock_paper_scissors_games:
            await ctx.send(
                "You already have an active Rock Paper Scissors game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        wager = None
        if amount is not None:
            wager_amount = int(cast(Any, amount))
            if wager_amount < GAME_MIN_BID:
                await ctx.send(
                    f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                wager = await self.bot.currency.open_wager(
                    ctx.author.id,
                    wager_amount,
                    source="rock_paper_scissors",
                )
            except InvalidAmount:
                await ctx.send(
                    f"Coin wagers must be at least **{GAME_MIN_BID:,} Coins**.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except InsufficientFunds as error:
                if error.balance == 0:
                    message = (
                        "Your wallet is empty, so you can't place that wager yet. "
                        "Use `daily` or `weekly` to collect Coins first."
                    )
                else:
                    message = (
                        f"You only have **{error.balance:,} Coins**, so you can't "
                        f"wager **{error.required:,} Coins**."
                    )
                await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
                return

        game = RockPaperScissorsGame(
            ctx.author.id,
            always_win=ctx.author.id == RPS_ALWAYS_WIN_USER_ID,
            wager_id=wager.id if wager is not None else None,
            stake=wager.stake if wager is not None else 0,
            payout_limit=MAX_WAGER_PAYOUT,
        )
        view = RockPaperScissorsWagerView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_rock_paper_scissors,
            on_progress=self._record_rock_paper_scissors_progress,
            on_cash_out=self._cash_out_rock_paper_scissors,
        )
        self._rock_paper_scissors_games[ctx.author.id] = game
        try:
            view.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            self._rock_paper_scissors_games.pop(ctx.author.id, None)
            view.stop()
            if wager is not None:
                try:
                    await self.bot.currency.settle_wager(wager.id, wager.stake)
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund a Rock Paper Scissors wager after startup failed"
                    )
            raise

    async def _finish_rock_paper_scissors(
        self, game: StreakGame, _timed_out: bool
    ) -> None:
        active = self._rock_paper_scissors_games.get(game.user_id)
        if active is not game:
            return
        if isinstance(game, RockPaperScissorsGame):
            if not game.cashed_out and game.wager_id is not None:
                try:
                    await self.bot.currency.settle_wager(game.wager_id, 0)
                except Exception:
                    self.bot.logger.exception(
                        "Failed to settle a lost Rock Paper Scissors wager"
                    )
        self._rock_paper_scissors_games.pop(game.user_id, None)
        await self._record_rock_paper_scissors_progress(game)

    async def _cash_out_rock_paper_scissors(self, game: RockPaperScissorsGame) -> bool:
        # Keep this guard at the settlement boundary as well as in the view:
        # callbacks can be invoked by restored/persistent views or tests, so a
        # wager must never be paid out before the player has made a move.
        if game.wager_id is None or game.last_choice is None:
            game.cash_out_error = "Make a move before cashing out your wager."
            return False
        try:
            settlement = await self.bot.currency.settle_wager(
                game.wager_id, game.potential_payout
            )
        except BalanceOverflow:
            game.cash_out_error = (
                "Your wallet is too close to the Coin limit to hold this payout."
            )
            return False
        except Exception:
            self.bot.logger.exception("Failed to cash out a Rock Paper Scissors wager")
            return False
        if settlement.wager.status != "cashed_out":
            game.cash_out_error = (
                "That wager has already ended and cannot be cashed out."
            )
            return False
        game.potential_payout = settlement.wager.payout
        return True

    async def _send_streak_game_stats(
        self,
        ctx: Context,
        game_name: str,
        title: str,
        user: discord.User,
    ) -> None:
        visible = self.bot.db_cache.game_history_visible_to(user.id, ctx.author.id)
        highest = None
        if visible:
            highest = await self.bot.pool.fetchrow(
                "SELECT highest_streak, highest_streak_payout "
                "FROM streak_game_stats "
                "WHERE game = $1 AND user_id = $2",
                game_name,
                user.id,
            )
        rows = await self.bot.pool.fetch(
            "SELECT user_id, highest_streak, highest_streak_payout, "
            "highest_payout, highest_payout_streak "
            "FROM streak_game_stats WHERE game = $1 "
            "ORDER BY highest_streak DESC, updated_at ASC, user_id ASC",
            game_name,
        )
        rows = [
            row
            for row in rows
            if self.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ]
        # Apply privacy before limiting, otherwise a private record can hide
        # an eligible user from the visible top five.
        streak_rows = rows[:5]
        payout_rows = sorted(
            (row for row in rows if int(row["highest_payout"] or 0) > 0),
            key=lambda row: (
                -int(row["highest_payout"] or 0),
                -int(row["highest_payout_streak"] or 0),
                int(row["user_id"]),
            ),
        )[:5]
        safe_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(user.name)
        )
        if highest is None:
            highest_streak = 0
            highest_streak_payout = 0
        else:
            highest_streak = int(highest["highest_streak"] or 0)
            highest_streak_payout = int(highest["highest_streak_payout"] or 0)
        highest_line = f"**Highest streak:** {highest_streak:,}"
        if highest_streak_payout > 0:
            highest_line += f" ({highest_streak_payout:,} Coins)"
        lines = [
            f"## {title} stats for {safe_name}",
            highest_line,
            "### Highest streaks",
        ]
        if streak_rows:
            for index, row in enumerate(streak_rows, start=1):
                listed_user = await get_or_fetch_user(self.bot, int(row["user_id"]))
                listed_name = getattr(listed_user, "name", None) or str(row["user_id"])
                safe_listed_name = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(listed_name)
                )
                line = (
                    f"**#{index} {safe_listed_name}** · {int(row['highest_streak']):,}"
                )
                payout = int(row["highest_streak_payout"] or 0)
                if payout > 0:
                    line += f" ({payout:,} Coins)"
                lines.append(line)
        else:
            lines.append("No streaks have been recorded yet.")

        lines.extend(("", "### Highest payouts"))
        if payout_rows:
            for index, row in enumerate(payout_rows, start=1):
                listed_user = await get_or_fetch_user(self.bot, int(row["user_id"]))
                listed_name = getattr(listed_user, "name", None) or str(row["user_id"])
                safe_listed_name = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(listed_name)
                )
                lines.append(
                    f"**#{index} {safe_listed_name}** · "
                    f"{int(row['highest_payout']):,} Coins "
                    f"({int(row['highest_payout_streak']):,} streak)"
                )
        else:
            lines.append("No payouts have been recorded yet.")

        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(lines)),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def _save_wordle_settings(
        self, user_id: int, hard_mode: bool, colourblind_mode: bool
    ) -> None:
        await save_wordle_settings(self.bot.pool, user_id, hard_mode, colourblind_mode)

    def _wordle_key(self, game: WordleGame) -> tuple[int, int]:
        return game.user_id, game.channel_id

    async def _start_wordle(self, ctx: Context) -> None:
        key = (ctx.author.id, ctx.channel.id)
        if key in self._wordle_games:
            await ctx.send(
                "You already have an active Wordle game in this channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        hard_mode, colourblind_mode = await get_wordle_settings(
            self.bot.pool, ctx.author.id
        )
        game = new_wordle_game(
            user_id=ctx.author.id,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            hard_mode=hard_mode,
            colourblind_mode=colourblind_mode,
        )
        view = WordleBoardView(
            game,
            self._wordle_interaction_guess,
            on_timeout=self._finish_wordle_timeout,
            on_give_up=self._wordle_interaction_give_up,
        )
        game.view = view
        self._wordle_games[key] = game
        try:
            game.message = await ctx.send(
                view=view,
                file=view.board_file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._wordle_games.pop(key, None)
            view.stop()
            raise

    async def _wordle_interaction_guess(
        self,
        interaction: discord.Interaction,
        game: WordleGame,
        guess: str,
    ) -> None:
        await self._submit_wordle_guess(game, guess, interaction=interaction)

    async def _wordle_interaction_give_up(
        self,
        interaction: discord.Interaction,
        game: WordleGame,
    ) -> None:
        """End a Wordle game as a loss when its owner gives up."""

        async with game.lock:
            if self._wordle_games.get(self._wordle_key(game)) is not game:
                await interaction.response.send_message(
                    "This Wordle game is no longer active.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if game.completed:
                await interaction.response.send_message(
                    "This Wordle game is already over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            game.result = "lost"
            view = game.view
            if not isinstance(view, WordleBoardView):
                await interaction.response.send_message(
                    "This Wordle game is no longer active.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            view.refresh()
            view.stop()
            self._wordle_games.pop(self._wordle_key(game), None)
            await interaction.response.edit_message(
                view=view,
                attachments=[view.board_file],
                allowed_mentions=discord.AllowedMentions.none(),
            )

            if self.bot.db_cache.user_game_tracking_enabled(game.user_id):
                await record_wordle_result(self.bot.pool, game)

    async def _submit_wordle_guess(
        self,
        game: WordleGame,
        guess: str,
        *,
        interaction: discord.Interaction | None = None,
        message: discord.Message | None = None,
    ) -> bool:
        async with game.lock:
            if self._wordle_games.get(self._wordle_key(game)) is not game:
                if interaction is not None:
                    await interaction.response.send_message(
                        "This Wordle game is no longer active.", ephemeral=True
                    )
                return False
            try:
                game.submit(guess, set(WORDLE_WORDS))
            except ValueError as error:
                if interaction is not None:
                    await interaction.response.send_message(str(error), ephemeral=True)
                elif message is not None:
                    try:
                        await message.reply(
                            str(error),
                            delete_after=5,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        pass
                return False

            view = game.view
            if not isinstance(view, WordleBoardView):
                return False
            view.refresh()
            attachment = view.board_file
            finished = game.completed
            if finished:
                view.stop()
                self._wordle_games.pop(self._wordle_key(game), None)
            if interaction is not None:
                await interaction.response.edit_message(
                    view=view,
                    attachments=[attachment],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            elif game.message is not None:
                try:
                    await game.message.edit(
                        view=view,
                        attachments=[attachment],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
            if finished and self.bot.db_cache.user_game_tracking_enabled(game.user_id):
                await record_wordle_result(self.bot.pool, game)
            return True

    async def _finish_wordle_timeout(self, game: WordleGame) -> None:
        key = self._wordle_key(game)
        if self._wordle_games.get(key) is not game:
            return
        self._wordle_games.pop(key, None)
        view = game.view
        if isinstance(view, WordleBoardView):
            view.refresh()
            if game.message is not None:
                try:
                    await game.message.edit(
                        view=view,
                        attachments=[view.board_file],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        if self.bot.db_cache.user_game_tracking_enabled(game.user_id):
            await record_wordle_result(self.bot.pool, game)

    async def _start_memory(self, ctx: Context) -> None:
        if ctx.author.id in self._memory_games:
            await ctx.send(
                "You already have an active Memory game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        game = MemoryGame(user_id=ctx.author.id)
        view = MemoryView(ctx, game, on_finish=self._finish_memory)
        self._memory_games[ctx.author.id] = game
        try:
            view.message = await ctx.send(
                "## Memory\nMatch all eight pairs.",
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._memory_games.pop(ctx.author.id, None)
            view.stop()
            raise

    async def _finish_memory(self, game: MemoryGame) -> None:
        if self._memory_games.get(game.user_id) is game:
            self._memory_games.pop(game.user_id, None)

    @commands.command(name="unscramble")
    async def unscramble(
        self,
        ctx: Context,
        difficulty: str = commands.param(
            default="random",
            description="Difficulty: easy, normal, hard, or random.",
        ),
    ) -> None:
        """Unscramble a word before the 60 second timer expires."""
        await self._start_unscramble(ctx, difficulty)

    async def _start_color_memorize(self, ctx: Context, difficulty: str) -> None:
        difficulty = difficulty.casefold().strip()
        if difficulty not in COLOR_MEMORIZE_DIFFICULTIES:
            choices = ", ".join(COLOR_MEMORIZE_DIFFICULTIES)
            raise commands.BadArgument(f"Choose one of: {choices}.")
        if ctx.author.id in self._color_memorize_games:
            await ctx.send("You already have an active Color Memorize game.")
            return
        palette_size, input_count = COLOR_MEMORIZE_DIFFICULTIES[difficulty]
        palette = tuple(COLOR_MEMORIZE_EMOJIS)[:palette_size]
        sequence = tuple(random.choice(palette) for _ in range(input_count))
        game = ColorMemorizeGame(
            user_id=ctx.author.id,
            difficulty=difficulty,
            palette=palette,
            sequence=sequence,
        )
        view = ColorMemorizeView(self, game)
        game.view = view
        self._color_memorize_games[game.user_id] = game
        try:
            game.message = await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            game.animation_task = asyncio.create_task(self._run_color_memorize(game))
        except Exception:
            self._color_memorize_games.pop(game.user_id, None)
            raise

    async def _edit_color_memorize(self, game: ColorMemorizeGame) -> None:
        if game.message is None:
            return
        if isinstance(game.view, ColorMemorizeView):
            game.view.refresh()
        try:
            await game.message.edit(view=game.view)
        except discord.HTTPException:
            self.bot.logger.debug(
                "Could not update Color Memorize message", exc_info=True
            )

    async def _run_color_memorize(self, game: ColorMemorizeGame) -> None:
        try:
            for count in (3, 2, 1):
                game.phase = "countdown"
                game.countdown = count
                game.highlight = None
                await self._edit_color_memorize(game)
                await asyncio.sleep(1)
            game.phase = "showing"
            game.countdown = None
            for color in game.sequence:
                game.highlight = color
                await self._edit_color_memorize(game)
                await asyncio.sleep(0.8)
                game.highlight = None
                await self._edit_color_memorize(game)
                await asyncio.sleep(0.15)
            game.phase = "input"
            game.highlight = None
            await self._edit_color_memorize(game)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Color Memorize sequence failed")
            await self._finish_color_memorize(game, won=False, timed_out=True)

    async def _color_memorize_guess(
        self,
        interaction: discord.Interaction,
        game: ColorMemorizeGame,
        color: str,
    ) -> None:
        async with game.lock:
            if game.phase != "input":
                await interaction.response.send_message(
                    "This game is not accepting input right now.", ephemeral=True
                )
                return
            expected = game.sequence[game.position]
            if color != expected:
                await interaction.response.defer()
                await self._finish_color_memorize(game, won=False)
                return
            game.position += 1
            if game.position >= len(game.sequence):
                await interaction.response.defer()
                await self._finish_color_memorize(game, won=True)
                return
            if isinstance(game.view, ColorMemorizeView):
                game.view.refresh()
            await interaction.response.edit_message(view=game.view)

    async def _record_minigame(
        self,
        game_name: str,
        user_id: int,
        difficulty: str,
        *,
        wins: int = 0,
        fails: int = 0,
    ) -> None:
        if not self.bot.db_cache.user_game_tracking_enabled(user_id):
            return
        try:
            await self.bot.pool.execute(
                "INSERT INTO minigame_stats (game, user_id, difficulty, wins, fails) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (game, user_id, difficulty) DO UPDATE SET "
                "wins = minigame_stats.wins + EXCLUDED.wins, "
                "fails = minigame_stats.fails + EXCLUDED.fails",
                game_name,
                user_id,
                difficulty,
                wins,
                fails,
            )
        except Exception:
            self.bot.logger.exception("Failed to record %s stats", game_name)

    async def _finish_color_memorize(
        self,
        game: ColorMemorizeGame,
        *,
        won: bool,
        timed_out: bool = False,
    ) -> None:
        if self._color_memorize_games.get(game.user_id) is not game:
            return
        self._color_memorize_games.pop(game.user_id, None)
        current = asyncio.current_task()
        if game.animation_task and game.animation_task is not current:
            game.animation_task.cancel()
        if isinstance(game.view, ColorMemorizeView):
            game.view.stop()
        game.phase = "won" if won else "failed"
        game.highlight = None
        game.countdown = None
        game.result = (
            "You remembered the entire sequence!"
            if won
            else (
                "You lost. Time ran out."
                if timed_out
                else "You lost. That was not the right sequence."
            )
        )
        await self._record_minigame(
            "color_memorize",
            game.user_id,
            game.difficulty,
            wins=1 if won else 0,
            fails=0 if won else 1,
        )
        if game.message is not None:
            try:
                if isinstance(game.view, ColorMemorizeView):
                    game.view.refresh()
                await game.message.edit(view=game.view)
            except discord.HTTPException:
                pass

    async def _start_unscramble(self, ctx: Context, difficulty: str) -> None:
        difficulty = difficulty.casefold().strip()
        if difficulty == "random":
            difficulty = random.choice(tuple(UNSCRAMBLE_WORDS))
        if difficulty not in UNSCRAMBLE_WORDS:
            raise commands.BadArgument("Choose easy, normal, hard, or random.")
        if ctx.channel.id in self._unscramble_games:
            await ctx.send(
                "There is already an active unscramble game in this channel."
            )
            return
        word = random.choice(UNSCRAMBLE_WORDS[difficulty])
        game = UnscrambleGame(
            channel_id=ctx.channel.id,
            difficulty=difficulty,
            word=word,
            scrambled=scramble_word(word),
            author_id=ctx.author.id,
        )
        game.view = UnscrambleView(self, game)
        self._unscramble_games[game.channel_id] = game
        try:
            game.message = await ctx.send(
                unscramble_content(game),
                view=game.view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            game.timeout_task = asyncio.create_task(self._unscramble_timeout(game))
        except Exception:
            self._unscramble_games.pop(game.channel_id, None)
            raise

    async def _unscramble_timeout(self, game: UnscrambleGame) -> None:
        try:
            await asyncio.sleep(60)
            async with game.lock:
                if self._unscramble_games.get(game.channel_id) is not game:
                    return
                self._unscramble_games.pop(game.channel_id, None)
                if isinstance(game.view, UnscrambleView):
                    game.view.disable_all()
                if game.message is not None:
                    await game.message.edit(
                        content=(
                            f"## Unscramble • {game.difficulty.title()}\n"
                            f"Time ran out. The word was **`{game.word}`**."
                        ),
                        view=game.view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except asyncio.CancelledError:
            raise
        except discord.HTTPException:
            pass

    @commands.Cog.listener("on_message")
    async def unscramble_listener(self, message: discord.Message) -> None:
        if is_legacy_instance(getattr(self, "bot", None)):
            return
        if message.author.bot:
            return
        game = self._unscramble_games.get(message.channel.id)
        if game is None or message.content.strip().casefold() != game.word.casefold():
            return
        async with game.lock:
            if self._unscramble_games.get(message.channel.id) is not game:
                return
            self._unscramble_games.pop(message.channel.id, None)
            if game.timeout_task is not None:
                game.timeout_task.cancel()
            if isinstance(game.view, UnscrambleView):
                game.view.disable_all()
            await self._record_minigame(
                "unscramble", message.author.id, game.difficulty, wins=1
            )
            if game.message is not None:
                try:
                    await game.message.edit(
                        content=(
                            f"## Unscramble • {game.difficulty.title()}\n"
                            f"{message.author.mention} solved it! The word was **`{game.word}`**."
                        ),
                        view=game.view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

    @commands.Cog.listener("on_message")
    async def wordle_listener(self, message: discord.Message) -> None:
        """Accept only five-letter guesses from the active game's owner."""
        if is_legacy_instance(getattr(self, "bot", None)):
            return
        if message.author.bot:
            return
        key = (message.author.id, message.channel.id)
        game = self._wordle_games.get(key)
        if game is None:
            return
        guess = message_guess(message, game)
        if guess is None:
            return
        await self._submit_wordle_guess(game, guess, message=message)

    @commands.Cog.listener("on_message")
    async def crash_cashout_listener(self, message: discord.Message) -> None:
        """Allow a Crash author to cash out by typing a short alias.

        The active game is keyed by user, but the channel check is important:
        a message sent by the author in another channel must not settle the
        wager.  ``CrashView`` owns the lock and finish callback so this path
        is safe when a typed alias races the button or the crash task.
        """

        bot = getattr(self, "bot", None)
        if bot is not None and is_legacy_instance(bot):
            return
        if message.author.bot:
            return
        alias = " ".join(message.content.casefold().split())
        if alias not in CRASH_CASHOUT_ALIASES:
            return

        game = self._crash_games.get(message.author.id)
        if game is None or game.finished:
            return
        game_message = game.message
        if game_message is None or game_message.channel.id != message.channel.id:
            return
        view = game.view
        if not isinstance(view, CrashView):
            return
        try:
            await view.cash_out_from_message()
        except Exception:
            self.bot.logger.exception("Failed to cash out Crash game from message")

    def tictactoe_mode_view(self, ctx: Context, *, prompt: str | None = None):
        from .tictactoe import TicTacToeModeView

        return TicTacToeModeView(self._tictactoe_controller, ctx, prompt)

    def tictactoe_challenge_view(
        self,
        ctx: Context,
        user: discord.abc.User,
        *,
        challenger_bid: int | None = None,
    ):
        from .tictactoe import TicTacToeChallengeView

        return TicTacToeChallengeView(
            self._tictactoe_controller,
            ctx,
            user,
            challenger_bid=challenger_bid,
        )

    async def _phone_consent(self, user_id: int) -> bool | None:
        if user_id in self._phone_consent_cache:
            return self._phone_consent_cache[user_id]

        row = await self.bot.pool.fetchrow(
            "SELECT consented FROM phone_consent WHERE user_id = $1", user_id
        )
        consented = None if row is None else bool(row["consented"])
        self._phone_consent_cache[user_id] = consented
        return consented

    async def _save_phone_consent(self, user_id: int, consented: bool) -> None:
        await self.bot.pool.execute(
            """
            INSERT INTO phone_consent (user_id, consented)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET consented = EXCLUDED.consented
            """,
            user_id,
            consented,
        )
        self._phone_consent_cache[user_id] = consented

    async def _ensure_phone_consent(
        self,
        channel: discord.abc.Messageable,
        user_id: int,
        *,
        prompt_declined: bool,
    ) -> bool:
        status = await self._phone_consent(user_id)
        if status is True:
            return True
        if status is False and not prompt_declined:
            return False

        async with self._phone_consent_lock:
            if user_id in self._phone_consent_prompts:
                return False
            self._phone_consent_prompts.add(user_id)

        view = PhoneConsentView(self, user_id)
        try:
            view.message = await channel.send(
                "Before using Fishie Phone, please confirm: calls may be recorded "
                "in case of reports to make sure everyone is safe while using the "
                "Fishie Phone.",
                view=view,
            )
            await view.wait()
            return view.result is True
        except discord.HTTPException:
            return False
        finally:
            async with self._phone_consent_lock:
                self._phone_consent_prompts.discard(user_id)

    def _phone_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None

    @staticmethod
    def _format_phone_logs(connection: PhoneConnection) -> str:
        grouped: dict[tuple[int, int], list[PhoneLogEntry]] = {}
        for entry in connection.logs:
            grouped.setdefault((entry.guild_id, entry.channel_id), []).append(entry)

        sections: list[str] = []
        for (guild_id, channel_id), entries in grouped.items():
            lines = [f"[{guild_id}] - [{channel_id}]", ""]
            lines.extend(
                f"{entry.username} {entry.user_id} {entry.message_id} - {entry.content}"
                for entry in entries
            )
            sections.append("\n".join(lines))
        report = "\n\n".join(sections) or "No messages were sent during this call."
        if connection.logs_truncated:
            report += "\n\n[Transcript truncated to protect resources.]"
        return report

    async def _report_phone_session(self, connection: PhoneConnection) -> bool:
        async with self._phone_lock:
            if connection.reported:
                return False
            connection.reported = True

        try:
            report = self._format_phone_logs(connection).encode("utf-8")
            file = discord.File(BytesIO(report), filename="phone-report.txt")
            await self.phone_logs.send(file=file, username="Fishie Phone Reports")
        except Exception:
            async with self._phone_lock:
                connection.reported = False
            raise
        return True

    async def _expire_phone_archive(self, connection: PhoneConnection) -> None:
        await asyncio.sleep(300)
        self._phone_archives.pop(id(connection), None)
        connection.logs.clear()

    async def _close_phone(self, connection: PhoneConnection, *, reason: str) -> bool:
        async with self._phone_lock:
            removed = False
            for channel_id in connection.channel_ids:
                if self._phone_connections.get(channel_id) is connection:
                    self._phone_connections.pop(channel_id, None)
                    removed = True

        if not removed:
            return False

        current_task = asyncio.current_task()
        if connection.idle_task and connection.idle_task is not current_task:
            connection.idle_task.cancel()

        self._phone_archives[id(connection)] = connection
        connection.cleanup_task = asyncio.create_task(
            self._expire_phone_archive(connection)
        )

        for channel_id in connection.channel_ids:
            channel = self._phone_channel(channel_id)
            if channel is not None:
                try:
                    view = PhoneReportView(self, connection)
                    view.message = await channel.send(reason, view=view)
                except discord.HTTPException:
                    pass
        return True

    async def _phone_idle_watch(self, connection: PhoneConnection) -> None:
        while True:
            delay = max(
                0.0,
                connection.last_activity
                + PHONE_IDLE_TIMEOUT
                - asyncio.get_running_loop().time(),
            )
            await asyncio.sleep(delay)
            if (
                asyncio.get_running_loop().time() - connection.last_activity
                < PHONE_IDLE_TIMEOUT
            ):
                continue
            await self._close_phone(
                connection,
                reason="☎️ This phone call ended after 60 seconds without any messages.",
            )
            return

    @staticmethod
    async def _is_phone_command(bot: Fishie, message: discord.Message) -> bool:
        context = await bot.get_context(message)
        command = context.command
        return bool(command and command.name in {"phone", "hangup"})

    @commands.hybrid_command(
        name="phone",
        aliases=("ring", "userphone", "call", "fishiephone", "fishphone"),
        extras={"usage": "[-onlyme/-om/-private]"},
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def phone(self, ctx: Context, *, flags: PhoneFlags):
        """Ring for a user in another server and connect the two channels.

        -# -onlyme/-om/-private  Only relay messages sent by you from this channel.
        """

        if ctx.guild is None:
            return

        is_nsfw = getattr(ctx.channel, "is_nsfw", None)
        if callable(is_nsfw) and is_nsfw():
            await ctx.send("The phone command cannot be used in an NSFW channel.")
            return

        if not await self._ensure_phone_consent(
            ctx.channel, ctx.author.id, prompt_declined=True
        ):
            return

        if ctx.channel.id in self._phone_connections:
            await ctx.send("This channel is already connected to another phone call.")
            return

        already_ringing = False
        match: PhoneRinger | None = None
        async with self._phone_lock:
            already_ringing = any(
                r.author_id == ctx.author.id for r in self._phone_ringers
            )
            if not already_ringing:
                match = next(
                    (
                        r
                        for r in self._phone_ringers
                        if r.guild_id != ctx.guild.id and r.author_id != ctx.author.id
                    ),
                    None,
                )
                if match is not None:
                    self._phone_ringers.remove(match)
                    connection = PhoneConnection(
                        channel_ids=(match.channel_id, ctx.channel.id),
                        user_ids=(match.author_id, ctx.author.id),
                        onlyme=(match.onlyme, flags.onlyme),
                        last_activity=asyncio.get_running_loop().time(),
                    )
                    self._phone_connections[match.channel_id] = connection
                    self._phone_connections[ctx.channel.id] = connection
                    connection.idle_task = asyncio.create_task(
                        self._phone_idle_watch(connection)
                    )
                    match.event.set()
                else:
                    ringer = PhoneRinger(
                        author_id=ctx.author.id,
                        guild_id=ctx.guild.id,
                        channel_id=ctx.channel.id,
                        channel=ctx.channel,
                        event=asyncio.Event(),
                        onlyme=flags.onlyme,
                    )
                    self._phone_ringers.append(ringer)

        if already_ringing:
            await ctx.send("You are already ringing for a phone call.")
            return

        if match is not None:
            await match.channel.send(
                "☎️ Connected! Messages from this channel will now be relayed."
            )
            await ctx.send(
                "☎️ Connected! Messages from this channel will now be relayed."
            )
            return

        mode = (
            "Only your messages will be relayed."
            if flags.onlyme
            else "Messages from everyone in this channel will be relayed."
        )
        await ctx.send(
            f"☎️ Ringing for a user in another server for 60 seconds. {mode}"
        )

        try:
            await asyncio.wait_for(ringer.event.wait(), timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("☎️ Nobody answered your call.")
        finally:
            async with self._phone_lock:
                if ringer in self._phone_ringers:
                    self._phone_ringers.remove(ringer)

    @commands.hybrid_command(name="hangup", aliases=("end",))
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def hangup(self, ctx: Context) -> None:
        """End your active phone call or cancel your unanswered ring."""

        if ctx.guild is None:
            return

        async with self._phone_lock:
            ringer = next(
                (r for r in self._phone_ringers if r.author_id == ctx.author.id),
                None,
            )
            if ringer is not None:
                self._phone_ringers.remove(ringer)
                ringer.event.set()
            connection = self._phone_connections.get(ctx.channel.id)

        if connection is not None:
            await self._close_phone(connection, reason="☎️ Phone call ended.")
            return

        if ringer is not None:
            await ctx.send("☎️ Ring cancelled.")
        else:
            await ctx.send("☎️ You do not have an active phone call.")

    @commands.Cog.listener("on_message")
    async def phone_relay(self, message: discord.Message) -> None:
        if is_legacy_instance(getattr(self, "bot", None)):
            return
        if message.guild is None or message.author.bot:
            return

        if await self._is_phone_command(self.bot, message):
            return

        connection = self._phone_connections.get(message.channel.id)
        if connection is None:
            return

        if not await self._ensure_phone_consent(
            message.channel, message.author.id, prompt_declined=False
        ):
            return

        try:
            source_index = connection.channel_ids.index(message.channel.id)
        except ValueError:
            return

        if connection.onlyme[source_index] and (
            message.author.id != connection.user_ids[source_index]
        ):
            return

        target_channel_id = connection.channel_ids[1 - source_index]
        target = self.bot.get_channel(target_channel_id)
        if target is None:
            try:
                target = await self.bot.fetch_channel(target_channel_id)
            except (discord.HTTPException, discord.NotFound):
                return
        if not isinstance(target, discord.abc.Messageable):
            return

        connection.last_activity = asyncio.get_running_loop().time()
        author_name = discord.utils.escape_markdown(message.author.name)
        attachment_links = [attachment.url for attachment in message.attachments]
        logged_content = message.content
        if attachment_links:
            logged_content += "".join(f"\n{url}" for url in attachment_links)
        original_log_length = len(logged_content)
        if (
            len(connection.logs) < PHONE_LOG_MAX_ENTRIES
            and connection.log_bytes < PHONE_LOG_MAX_BYTES
        ):
            remaining = PHONE_LOG_MAX_BYTES - connection.log_bytes
            encoded_content = logged_content.encode("utf-8")[:remaining]
            logged_content = encoded_content.decode("utf-8", "ignore")
            connection.logs.append(
                PhoneLogEntry(
                    guild_id=message.guild.id,
                    username=message.author.name,
                    user_id=message.author.id,
                    message_id=message.id,
                    channel_id=message.channel.id,
                    content=logged_content,
                )
            )
            connection.log_bytes += len(encoded_content)
            if len(logged_content) < original_log_length:
                connection.logs_truncated = True
        else:
            connection.logs_truncated = True

        content = f"**{author_name}**"
        if message.content:
            safe_content = discord.utils.escape_mentions(
                discord.utils.escape_markdown(message.content)
            )
            content += f": {safe_content}"
        if attachment_links:
            content += "".join(f"\n{url}" for url in attachment_links)

        try:
            await target.send(
                content=content[:2000],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    @commands.command(name="8ball")
    async def _8ball(
        self,
        ctx: Context,
        *,
        question: str = commands.param(
            displayed_name="question", description="What shall you ask?"
        ),
    ):
        """Ask the magic 8-ball a question.

        tony wanted this command"""

        await self._ask_eight_ball(ctx, question)

    async def _ask_eight_ball(self, ctx: Context, question: str) -> None:
        """Send a response from the magic 8-ball."""

        answers = (
            "It is certain.",
            "It is decidedly so.",
            "Without a doubt.",
            "Yes definitely.",
            "You may rely on it.",
            "As I see it, yes.",
            "Most likely.",
            "Outlook good.",
            "Yes.",
            "Signs point to yes.",
            "Reply hazy, try again.",
            "Ask again later.",
            "Better not tell you now.",
            "Cannot predict now.",
            "Concentrate and ask again.",
            "Don't count on it.",
            "My reply is no.",
            "My sources say no.",
            "Outlook not so good.",
            "Very doubtful.",
            "prolly",
            "prolly not",
            "cheese beast",
            "are you him?",
            "himmers bro",
            "ur mic is on btw",
            "u might be muted",
            "ur a beast",
            "ur him",
            "BANG",
            "sure man",
        )

        msg = random.choice(answers)
        if ctx.interaction:
            msg += f"\n-# {ctx.author.display_name} asked: *{question}*"

        await ctx.send(msg)

    # @commands.command(name="wtp", hidden=True, enabled=False)
    # async def wtp(self, ctx: Context):
    #     """Start a Who's That Pokémon guessing game."""
    #     await ctx.typing()

    #     data = await dagpi(self.bot, ctx.message, "https://api.dagpi.xyz/data/wtp")

    #     embed = discord.Embed(color=self.bot.embedcolor)
    #     embed.set_author(name="Who's that pokemon?")

    #     image = await to_image(ctx.session, data["question"])
    #     file = discord.File(fp=image, filename="pokemon.png")

    #     embed.set_image(url="attachment://pokemon.png")

    #     await ctx.send(embed=embed, file=file, view=WTPView(ctx, data))

    @commands.command(
        name="badapple",
        aliases=(
            "ba",
            "bad apple",
        ),
    )
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def badapple(self, ctx: Context):
        """Bad Apple!! feat.nomico"""

        async with ctx.typing():
            await ctx.send(
                file=discord.File(
                    FILES_ROOT / "videos" / "bad apple.mp4",
                    filename=f"fishie_loves_{ctx.author.name}.mp4",
                )
            )

    @commands.command(
        name="quoteisifyouhaveaproblemwithmetextmeandifyoudonthavemynumberyoudontknowmewellenoughtohaveaproblemwithme",
        aliases=("QIIYHAPWMTMAIYDHMNYDKMWETHAPWM",),
        hidden=True,
    )
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def QIIYHAPWMTMAIYDHMNYDKMWETHAPWM(self, ctx: Context):
        """if you have a problem with me text me and if you dont have my number you dont know me well enough to have a problem with me"""

        async with ctx.typing():
            await ctx.send(
                file=discord.File(
                    FILES_ROOT / "videos" / "QIIYHAPWMTMAIYDHMNYDKMWETHAPWM.mp4",
                    filename=f"fishie_loves_{ctx.author.name}.mp4",
                )
            )

    @commands.command(name="echo")
    async def echo(self, ctx: Context, *, text: str):
        """Repeat the provided text without allowing mentions."""
        await ctx.send(text, allowed_mentions=None)

    async def cog_load(self) -> None:
        if is_legacy_instance(getattr(self, "bot", None)):
            self.bot.logger.info(
                "Skipping shared click-reward flusher on legacy bot instance"
            )
            return
        self.bot.currency.set_click_reward_flusher(self.flush_click_rewards)
        self.flush_click_cache_loop.start()
        self.birthday_reward_loop.start()

    def cog_unload(self) -> None:
        """Stop background minigame timers when the fun extension reloads."""
        self.bot.currency.set_click_reward_flusher(None)
        self.flush_click_cache_loop.cancel()
        self.birthday_reward_loop.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self.flush_click_cache())
        self.unregister_post_views()
        self.unregister_video_views()
        self._connectfour_controller.close()
        self._stop_wordbomb_games()
        self._stop_lastletter_games()
        for game in self._color_memorize_games.values():
            if game.animation_task is not None:
                game.animation_task.cancel()
        for game in self._unscramble_games.values():
            if game.timeout_task is not None:
                game.timeout_task.cancel()
            if isinstance(game.view, UnscrambleView):
                game.view.stop()
        for game in self._2048_games.values():
            game.finished = True
        for game in self._lightsout_games.values():
            game.finished = True
            if game.view is not None:
                game.view.stop()
        for game in self._higher_or_lower_games.values():
            game.finished = True
            if game.view is not None:
                game.view.stop()
        for game in self._heads_or_tails_games.values():
            game.finished = True
            if game.view is not None:
                game.view.stop()
        for game in self._rock_paper_scissors_games.values():
            game.finished = True
            if game.view is not None:
                game.view.stop()
        for game in self._wordle_games.values():
            if isinstance(game.view, WordleBoardView):
                game.view.stop()
        for game in self._memory_games.values():
            game.finished = True
        for game in self._race_games.values():
            game.cancelled = True
            if game.lobby_task is not None:
                game.lobby_task.cancel()
            if game.race_task is not None:
                game.race_task.cancel()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for participant in game.players:
                if not participant.is_bot and not game.started and loop is not None:
                    loop.create_task(
                        self._refund_race_wager(participant.user_id, participant.wager)
                    )
            if game.view is not None:
                game.view.stop()
        for game in self._luckyroll_games.values():
            game.cancelled = True
            if game.lobby_task is not None:
                game.lobby_task.cancel()
            if game.roll_task is not None:
                game.roll_task.cancel()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for participant in game.players:
                if not participant.is_bot and not game.started and loop is not None:
                    loop.create_task(
                        self._refund_luckyroll_wager(
                            participant.user_id, participant.wager
                        )
                    )
            if game.view is not None:
                game.view.stop()
        for game in self._slots_games.values():
            if game.task is not None:
                game.task.cancel()
        for game in self._mines_games.values():
            game.timeout()
            if game.view is not None:
                game.view.stop()
        for game in self._blackjack_games.values():
            game.timeout()
            if game.view is not None:
                game.view.stop()
        for game in self._blackjack_duels.values():
            game.timeout()
            if game.view is not None:
                game.view.stop()
        for game in self._crash_games.values():
            game.finished = True
            if game.task is not None:
                game.task.cancel()
            if game.view is not None:
                game.view.stop()
        for duel in self._pvp_duels.values():
            duel.stop()
        self._pvp_duels.clear()
        DuelBidView._active.clear()
        self._color_memorize_games.clear()
        self._unscramble_games.clear()
        self._2048_games.clear()
        self._lightsout_games.clear()
        self._higher_or_lower_games.clear()
        self._heads_or_tails_games.clear()
        self._rock_paper_scissors_games.clear()
        self._wordle_games.clear()
        self._memory_games.clear()
        self._lastletter_games.clear()
        self._race_games.clear()
        self._luckyroll_games.clear()
        self._slots_games.clear()
        self._mines_games.clear()
        self._blackjack_games.clear()
        self._blackjack_duels.clear()
        self._blackjack_duel_wagers.clear()
        self._crash_games.clear()


# Mark metadata at import time as well as during cog construction.  This
# keeps API/help category discovery deterministic in tests and in tools that
# inspect commands before setup runs.
for _command in Fun.__cog_commands__:
    if _command.name in GAMES_HELP_COMMANDS:
        _command.extras["help_category"] = "Games"


async def setup(bot: Fishie):
    fun = Fun(bot)
    await fun.load_wordbomb_custom_words()
    await bot.add_cog(fun)
    await fun.register_video_views()
    await fun.register_post_views()
