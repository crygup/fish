from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

import discord
import psutil
from discord import app_commands
from discord.ext import commands

from utils import get_or_fetch_user, to_image
from utils.paths import FILES_ROOT

from .about import About
from .connectfour import (
    ConnectFourChallengeView,
    ConnectFourController,
    ConnectFourSetupView,
)
from .corn import Corn
from .game_2048 import Game2048, Game2048View, highest_tile
from .helpers import RPSView, WTPView, dagpi
from .lightsout import LightsOutGame, LightsOutView
from .memory import MemoryGame, MemoryView
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
from .reactions import ReactionStats
from .streak_games import (
    HeadsOrTailsGame,
    HeadsOrTailsView,
    HigherOrLowerGame,
    HigherOrLowerView,
    StreakGame,
)
from .tictactoe import TicTacToeController
from .video import VideoCommands
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
        self.footer = discord.ui.TextDisplay(
            "Anyone can press the button. Clicks are counted globally and per user."
        )
        self.button = discord.ui.Button(
            label="Click",
            style=discord.ButtonStyle.primary,
        )
        self.button.callback = self._click
        self.add_item(
            discord.ui.Container(
                self.counter,
                discord.ui.Separator(),
                self.footer,
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


class Fun(VideoCommands, About, Corn, ReactionStats):
    """Fun miscellaneous commands"""

    emoji = discord.PartialEmoji(name="\U0001f604")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
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
        self._wordle_games: dict[tuple[int, int], WordleGame] = {}
        self._memory_games: dict[int, MemoryGame] = {}
        self.phone_logs = discord.Webhook.from_url(
            self.bot.config["webhooks"]["phone_logs"], session=self.bot.session
        )
        self.process = psutil.Process()
        self.invite_url = discord.utils.oauth_url(
            self.bot.config["ids"]["bot_id"], permissions=self.bot.bot_permissions
        )
        self._tictactoe_controller = TicTacToeController(self)
        self._connectfour_controller = ConnectFourController(self)

    async def _click_total(self) -> int:
        value = await self.bot.pool.fetchval(
            "SELECT clicks FROM click_totals WHERE id = TRUE"
        )
        return int(value or 0)

    async def _record_click(self, user_id: int, guild_id: int | None) -> int:
        """Atomically increment the global, user, and optional guild counters."""
        if not self.bot.db_cache.user_game_tracking_enabled(user_id):
            # Keep the public board usable without writing a user's activity
            # after they disable game tracking.
            return await self._click_total()
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                total = await connection.fetchval(
                    "INSERT INTO click_totals (id, clicks) VALUES (TRUE, 1) "
                    "ON CONFLICT (id) DO UPDATE SET clicks = click_totals.clicks + 1 "
                    "RETURNING clicks"
                )
                await connection.execute(
                    "INSERT INTO click_user_totals (user_id, clicks) VALUES ($1, 1) "
                    "ON CONFLICT (user_id) DO UPDATE SET clicks = "
                    "click_user_totals.clicks + 1",
                    user_id,
                )
                if guild_id is not None:
                    await connection.execute(
                        "INSERT INTO click_guild_totals (guild_id, clicks) "
                        "VALUES ($1, 1) ON CONFLICT (guild_id) DO UPDATE SET "
                        "clicks = click_guild_totals.clicks + 1",
                        guild_id,
                    )
                    await connection.execute(
                        "INSERT INTO click_user_guild_totals "
                        "(user_id, guild_id, clicks) VALUES ($1, $2, 1) "
                        "ON CONFLICT (user_id, guild_id) DO UPDATE SET clicks = "
                        "click_user_guild_totals.clicks + 1",
                        user_id,
                        guild_id,
                    )
        return int(total or 0)

    async def _send_click_stats(self, ctx: Context, user: discord.User) -> None:
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
        fallback="play",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game(self, ctx: Context) -> None:
        """Play Fishie's games."""
        await ctx.send_help(ctx.command)

    @game.command(name="tic-tac-toe")
    @app_commands.describe(user="The user to challenge. Omit this to play Fishie.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_tic_tac_toe(
        self, ctx: Context, user: discord.User | None = None
    ) -> None:
        """Play Tic-Tac-Toe against another user or Fishie."""
        await self.tictactoe(ctx, user)

    @game.command(name="connect-four", aliases=("connect4", "c4", "connect"))
    @app_commands.describe(user="The user to challenge. Omit this to play Fishie.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_connect_four(
        self, ctx: Context, user: discord.User | None = None
    ) -> None:
        """Play Connect Four against another user or Fishie."""
        await self.connectfour(ctx, user)

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
    async def higher_or_lower(self, ctx: Context) -> None:
        """Guess whether each new card is higher or lower."""
        await self._start_higher_or_lower(ctx)

    @higher_or_lower.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def higher_or_lower_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Higher or Lower streak statistics."""
        await self._send_streak_game_stats(
            ctx, "higher_or_lower", "Higher or Lower", user
        )

    @game.command(name="higher-or-lower")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_higher_or_lower(self, ctx: Context) -> None:
        """Guess whether each new card is higher or lower."""
        await self._start_higher_or_lower(ctx)

    @commands.group(
        name="heads-or-tails",
        aliases=("headsortails", "headortail", "coinflip", "cf"),
        invoke_without_command=True,
    )
    async def heads_or_tails(self, ctx: Context) -> None:
        """Guess an endless series of coin flips."""
        await self._start_heads_or_tails(ctx)

    @heads_or_tails.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def heads_or_tails_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show Heads or Tails streak statistics."""
        await self._send_streak_game_stats(
            ctx, "heads_or_tails", "Heads or Tails", user
        )

    @game.command(name="heads-or-tails")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_heads_or_tails(self, ctx: Context) -> None:
        """Guess an endless series of coin flips."""
        await self._start_heads_or_tails(ctx)

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
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def game_rock_paper_scissors(self, ctx: Context) -> None:
        """Play rock paper scissors against Fishie."""
        await ctx.send(view=RPSView(ctx))

    @commands.command(name="rock-paper-scissors", aliases=("rockpaperscissors", "rps"))
    async def RPSCommand(self, ctx: Context):
        """
        Play rock paper scissors against me!
        """
        await ctx.send(view=RPSView(ctx))

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

    @commands.hybrid_command(name="invite", aliases=("join",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def invite(self, ctx: Context):
        """Sends a link to add me to a server."""

        await ctx.send(self.invite_url)

    @commands.hybrid_command(
        name="firstmessage",
        aliases=("firstmsg", "fmsg"),
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
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
    )
    async def tictactoe(self, ctx: Context, user: discord.User | None = None) -> None:
        """Play Tic-Tac-Toe against another user or Fishie."""

        bot_id = (
            ctx.bot.user.id
            if ctx.bot.user is not None
            else int(ctx.bot.config["ids"]["bot_id"])
        )
        if user is None or user.id == bot_id:
            view = self.tictactoe_mode_view(ctx)
            view.message = await ctx.send(
                (
                    "Mention a user to play against them, or choose a difficulty "
                    "below to play against Fishie."
                    if user is None
                    else "Choose a difficulty below to play against Fishie."
                ),
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

        view = self.tictactoe_challenge_view(ctx, user)
        view.message = await ctx.send(
            (
                f"{user.mention}, **{discord.utils.escape_markdown(user.display_name)}** "
                f"was challenged to Tic-Tac-Toe by "
                f"**{discord.utils.escape_markdown(ctx.author.display_name)}**. "
                "Do you want to play?"
            ),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tictactoe.command(name="stats", aliases=("leaderboard", "lb"))
    async def tictactoe_stats(self, ctx: Context) -> None:
        """Show global Tic-Tac-Toe win and loss leaderboards."""

        await self._tictactoe_controller.send_stats(ctx)

    @commands.group(
        name="connect4",
        aliases=("connect-four", "c4", "connect"),
        invoke_without_command=True,
    )
    async def connectfour(self, ctx: Context, user: discord.User | None = None) -> None:
        """Play Connect Four against another user or Fishie."""

        bot_id = (
            ctx.bot.user.id
            if ctx.bot.user is not None
            else int(ctx.bot.config["ids"]["bot_id"])
        )
        if user is None or user.id == bot_id:
            view = ConnectFourSetupView(self._connectfour_controller, ctx)
            view.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        if user.id == ctx.author.id:
            await ctx.send("You cannot play Connect Four against yourself.")
            return
        if user.bot:
            await ctx.send("You cannot challenge another bot to Connect Four.")
            return
        view = ConnectFourChallengeView(self._connectfour_controller, ctx, user)
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
            int(round(game.duration_seconds)),
            game.started_at,
        )

    async def _record_streak_game(
        self, game_name: str, tracking_key: str, game: StreakGame
    ) -> None:
        if not self.bot.db_cache.user_game_tracking_enabled(game.user_id):
            return
        try:
            await self.bot.pool.execute(
                """
                INSERT INTO streak_game_stats (game, user_id, highest_streak)
                VALUES ($1, $2, $3)
                ON CONFLICT (game, user_id) DO UPDATE SET
                    highest_streak = GREATEST(
                        streak_game_stats.highest_streak,
                        EXCLUDED.highest_streak
                    ),
                    updated_at = now()
                """,
                game_name,
                game.user_id,
                game.streak,
            )
        except Exception:
            self.bot.logger.exception("Failed to record %s streak", game_name)

    async def _record_higher_or_lower_progress(self, game: StreakGame) -> None:
        await self._record_streak_game("higher_or_lower", "higher_lower", game)

    async def _record_heads_or_tails_progress(self, game: StreakGame) -> None:
        await self._record_streak_game("heads_or_tails", "heads_tails", game)

    async def _start_higher_or_lower(self, ctx: Context) -> None:
        if ctx.author.id in self._higher_or_lower_games:
            await ctx.send(
                "You already have an active Higher or Lower game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        game = HigherOrLowerGame(ctx.author.id)
        view = HigherOrLowerView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_higher_or_lower,
            on_progress=self._record_higher_or_lower_progress,
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
            raise

    async def _finish_higher_or_lower(self, game: StreakGame, _timed_out: bool) -> None:
        active = self._higher_or_lower_games.get(game.user_id)
        if active is not game:
            return
        self._higher_or_lower_games.pop(game.user_id, None)
        await self._record_higher_or_lower_progress(game)

    async def _start_heads_or_tails(self, ctx: Context) -> None:
        if ctx.author.id in self._heads_or_tails_games:
            await ctx.send(
                "You already have an active Heads or Tails game.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        game = HeadsOrTailsGame(ctx.author.id)
        view = HeadsOrTailsView(
            game,
            accent_color=self.bot.embedcolor,
            on_finish=self._finish_heads_or_tails,
            on_progress=self._record_heads_or_tails_progress,
        )
        self._heads_or_tails_games[ctx.author.id] = game
        try:
            view.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            self._heads_or_tails_games.pop(ctx.author.id, None)
            view.stop()
            raise

    async def _finish_heads_or_tails(self, game: StreakGame, _timed_out: bool) -> None:
        active = self._heads_or_tails_games.get(game.user_id)
        if active is not game:
            return
        self._heads_or_tails_games.pop(game.user_id, None)
        await self._record_heads_or_tails_progress(game)

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
            highest = await self.bot.pool.fetchval(
                "SELECT highest_streak FROM streak_game_stats "
                "WHERE game = $1 AND user_id = $2",
                game_name,
                user.id,
            )
        rows = await self.bot.pool.fetch(
            "SELECT user_id, highest_streak FROM streak_game_stats "
            "WHERE game = $1 ORDER BY highest_streak DESC, updated_at ASC, "
            "user_id ASC LIMIT 5",
            game_name,
        )
        rows = [
            row
            for row in rows
            if self.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ]
        safe_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(user.name)
        )
        lines = [
            f"## {title} stats for {safe_name}",
            f"**Highest streak:** {int(highest or 0):,}",
            "### Highest streaks",
        ]
        if rows:
            for index, row in enumerate(rows, start=1):
                listed_user = await get_or_fetch_user(self.bot, int(row["user_id"]))
                listed_name = getattr(listed_user, "name", None) or str(row["user_id"])
                safe_listed_name = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(listed_name)
                )
                lines.append(
                    f"**#{index} {safe_listed_name}** · {int(row['highest_streak']):,}"
                )
        else:
            lines.append("No streaks have been recorded yet.")

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

    def tictactoe_mode_view(self, ctx: Context):
        from .tictactoe import TicTacToeModeView

        return TicTacToeModeView(self._tictactoe_controller, ctx)

    def tictactoe_challenge_view(self, ctx: Context, user: discord.abc.User):
        from .tictactoe import TicTacToeChallengeView

        return TicTacToeChallengeView(self._tictactoe_controller, ctx, user)

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

    def cog_unload(self) -> None:
        """Stop background minigame timers when the fun extension reloads."""
        self.unregister_video_views()
        self._connectfour_controller.close()
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
        for game in self._wordle_games.values():
            if isinstance(game.view, WordleBoardView):
                game.view.stop()
        for game in self._memory_games.values():
            game.finished = True
        self._color_memorize_games.clear()
        self._unscramble_games.clear()
        self._2048_games.clear()
        self._lightsout_games.clear()
        self._higher_or_lower_games.clear()
        self._heads_or_tails_games.clear()
        self._wordle_games.clear()
        self._memory_games.clear()


async def setup(bot: Fishie):
    fun = Fun(bot)
    await bot.add_cog(fun)
    await fun.register_video_views()
