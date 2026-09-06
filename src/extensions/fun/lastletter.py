"""LastLetter lobby and turn-based game implementation."""

from __future__ import annotations

import asyncio
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import discord
from discord.ext import commands

from utils import get_or_fetch_user

from .lastletter_stats import (
    award_lastletter_winner_coins,
    record_lastletter_result,
)
from .minigames import (
    add_correct_answer_reaction,
    is_valid_word_start_guess,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


LOBBY_TIMEOUT = 60.0
TURN_TIMEOUT = 15.0
MAX_GUESSES = 5
MAX_PLAYERS = 10
MIN_PLAYERS = 2
STARTING_LIVES = 2
MAX_PREFIX_LENGTH = 4
COINS_PER_OPPONENT = 1_000
ALPHABET = string.ascii_lowercase


@dataclass(slots=True)
class LastLetterPlayer:
    user_id: int
    name: str
    lives: int = STARTING_LIVES


@dataclass(slots=True)
class LastLetterGame:
    ctx: Context
    channel: discord.abc.Messageable
    channel_id: int
    guild_id: int | None
    host_id: int
    players: list[LastLetterPlayer]
    participants: tuple[int, ...] = ()
    token: str = field(default_factory=lambda: secrets.token_hex(4), repr=False)
    lobby_message: discord.Message | None = None
    lobby_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    lobby_view: LastLetterLobbyView | None = field(default=None, repr=False)
    letter_view: LastLetterLetterView | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    started_at: float = field(default_factory=time.monotonic, repr=False)
    turn_index: int = 0
    awaiting_letter: bool = True
    awaiting_player_id: int | None = None
    required_letters: str | None = None
    prefix_length: int = 1
    correct_since_reset: int = 0
    force_increase: bool = False
    started: bool = False
    finished: bool = False
    used_words: set[str] = field(default_factory=set, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _username(user: discord.abc.User) -> str:
    """Use usernames in the lobby instead of mutable display names."""

    return str(getattr(user, "name", None) or getattr(user, "display_name", user))


def _mention(user_id: int) -> str:
    return f"<@{user_id}>"


def normalize_lastletter_guess(content: str) -> str:
    """Normalize a message before checking whether it is a word guess."""

    return content.strip().casefold()


def is_valid_lastletter_guess(guess: str, letters: str) -> bool:
    """Return whether *guess* is a known word beginning with *letters*."""

    return is_valid_word_start_guess(guess, letters)


class LastLetterLobbyView(discord.ui.View):
    """Join/start controls for the LastLetter lobby."""

    def __init__(self, cog: LastLetterCommands, game: LastLetterGame):
        super().__init__(timeout=LOBBY_TIMEOUT)
        self.cog = cog
        self.game = game
        self.join_button = discord.ui.Button(
            label="Join / leave",
            style=discord.ButtonStyle.secondary,
            custom_id=f"lastletter:join:{game.channel_id}:{game.token}",
        )
        self.start_button = discord.ui.Button(
            label="Start game",
            style=discord.ButtonStyle.primary,
            custom_id=f"lastletter:start:{game.channel_id}:{game.token}",
        )
        self.join_button.callback = self._join
        self.start_button.callback = self._start
        self.add_item(self.join_button)
        self.add_item(self.start_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.finished or self.game.started:
            await interaction.response.send_message(
                "This LastLetter lobby has already started.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def _join(self, interaction: discord.Interaction) -> None:
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots cannot join LastLetter games.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with self.game.lock:
            if self.game.started or self.game.finished:
                await interaction.response.send_message(
                    "This LastLetter lobby has already started.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            existing = next(
                (
                    index
                    for index, player in enumerate(self.game.players)
                    if player.user_id == interaction.user.id
                ),
                None,
            )
            if existing is None:
                if len(self.game.players) >= MAX_PLAYERS:
                    await interaction.response.send_message(
                        f"LastLetter is limited to {MAX_PLAYERS} players.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                self.game.players.append(
                    LastLetterPlayer(
                        interaction.user.id,
                        _username(interaction.user),
                    )
                )
            else:
                self.game.players.pop(existing)
            await interaction.response.edit_message(
                embed=self.cog._lastletter_lobby_embed(self.game),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _start(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message(
                "Only the person who opened this lobby can start it early.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with self.game.lock:
            if len(self.game.players) < MIN_PLAYERS:
                await interaction.response.send_message(
                    "At least two players are required to start LastLetter.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.defer()
            self.game.lobby_event.set()

    async def on_timeout(self) -> None:
        self.game.lobby_event.set()


class LastLetterLetterView(discord.ui.View):
    """Four-letter picker shown at the start of each LastLetter interval."""

    def __init__(
        self,
        game: LastLetterGame,
        player: LastLetterPlayer,
        letters: tuple[str, ...],
    ) -> None:
        super().__init__(timeout=TURN_TIMEOUT)
        self.game = game
        self.player_id = player.user_id
        self.selected: str | None = None
        self.message: discord.Message | None = None
        self.buttons: dict[str, discord.ui.Button] = {}
        for letter in letters:
            button = discord.ui.Button(
                label=letter.upper(),
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"lastletter:letter:{game.channel_id}:{game.token}:{letter}"
                ),
            )
            button.callback = self._select_callback(letter)
            self.buttons[letter] = button
            self.add_item(button)

    def _select_callback(self, letter: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.player_id:
                await interaction.response.send_message(
                    "This LastLetter letter picker belongs to another player.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if (
                self.game.finished
                or not self.game.awaiting_letter
                or self.game.awaiting_player_id != self.player_id
            ):
                await interaction.response.send_message(
                    "This LastLetter turn is no longer active.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self.selected = letter
            for button in self.buttons.values():
                button.disabled = True
            await interaction.response.edit_message(view=self)
            self.stop()

        return callback

    async def on_timeout(self) -> None:
        for button in self.buttons.values():
            button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class LastLetterCommands:
    """Mixin containing the text LastLetter command and game engine."""

    bot: Fishie
    _lastletter_games: dict[int, LastLetterGame]

    @cast(Any, commands.group)(
        name="lastletter",
        aliases=("lastl", "last-letter"),
        invoke_without_command=True,
    )
    async def lastletter(self, ctx: Context) -> None:
        """Start a LastLetter lobby."""

        await self._start_lastletter(ctx)

    @lastletter.command(name="stats", aliases=("leaderboard", "top", "lb"))
    async def lastletter_stats(
        self, ctx: Context, user: discord.User = commands.Author
    ) -> None:
        """Show LastLetter wins, losses, and the global wins leaderboard."""

        await self._send_lastletter_stats(ctx, user)

    def _lastletter_lobby_embed(self, game: LastLetterGame) -> discord.Embed:
        players = "\n".join(
            f"{index}. {discord.utils.escape_markdown(player.name)} · "
            f"{player.lives} lives"
            for index, player in enumerate(game.players, start=1)
        )
        if not players:
            players = "No players have joined yet."
        embed = discord.Embed(
            title="LastLetter lobby",
            description=(
                "Join before the timer ends, then chain words by their last "
                "letters. Each player starts with two lives.\n\n"
                f"Players can join until the game starts (maximum {MAX_PLAYERS})."
            ),
            color=getattr(self.bot, "embedcolor", discord.Colour.blurple()),
        )
        embed.add_field(
            name=f"Players ({len(game.players)}/{MAX_PLAYERS})",
            value=players,
            inline=False,
        )
        embed.set_footer(
            text="The game starts automatically in 60 seconds, or when the host starts it."
        )
        return embed

    async def _start_lastletter(self, ctx: Context) -> None:
        if not await cast(Any, self)._require_currency_tracking(ctx):
            return
        games = self._lastletter_games
        channel_id = int(ctx.channel.id)
        active = games.get(channel_id)
        if active is not None and not active.finished:
            await ctx.send_new(
                "There is already a LastLetter game or lobby in this channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        game = LastLetterGame(
            ctx=ctx,
            channel=ctx.channel,
            channel_id=channel_id,
            guild_id=ctx.guild.id if ctx.guild else None,
            host_id=ctx.author.id,
            players=[LastLetterPlayer(ctx.author.id, _username(ctx.author))],
            participants=(ctx.author.id,),
        )
        view = LastLetterLobbyView(self, game)
        game.lobby_view = view
        # Reserve the channel before the first network await.  Otherwise two
        # commands arriving in the same event-loop turn can both observe an
        # empty slot and create overlapping lobbies.
        games[channel_id] = game
        try:
            game.lobby_message = await ctx.send(
                embed=self._lastletter_lobby_embed(game),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            if games.get(channel_id) is game:
                games.pop(channel_id, None)
            view.stop()
            raise
        game.task = asyncio.create_task(
            self._lastletter_wait_for_lobby(game),
            name=f"lastletter-lobby-{channel_id}",
        )

    async def _lastletter_wait_for_lobby(self, game: LastLetterGame) -> None:
        try:
            try:
                await asyncio.wait_for(game.lobby_event.wait(), LOBBY_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            await self._begin_lastletter(game)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception(
                "LastLetter lobby failed in channel %s", game.channel_id
            )
            await self._finish_lastletter(game, winner=None, record_results=False)

    async def _begin_lastletter(self, game: LastLetterGame) -> None:
        async with game.lock:
            if game.started or game.finished:
                return
            if game.lobby_view is not None:
                game.lobby_view.stop()
            if len(game.players) < MIN_PLAYERS:
                await self._edit_lastletter_lobby(
                    game,
                    "LastLetter needs at least two players to start.",
                    remove_view=True,
                )
                game.finished = True
                self._lastletter_games.pop(game.channel_id, None)
                return
            game.started = True
            game.started_at = time.monotonic()
            game.participants = tuple(player.user_id for player in game.players)
            game.turn_index = random.randrange(len(game.players))
            await self._edit_lastletter_lobby(
                game,
                f"LastLetter started with **{len(game.players)}** players.",
                remove_view=True,
            )
        game.task = asyncio.create_task(
            self._run_lastletter(game),
            name=f"lastletter-game-{game.channel_id}",
        )

    async def _edit_lastletter_lobby(
        self,
        game: LastLetterGame,
        status: str,
        *,
        remove_view: bool = False,
    ) -> None:
        if game.lobby_message is None:
            return
        embed = self._lastletter_lobby_embed(game)
        embed.description = f"{status}\n\n{embed.description}"
        try:
            await game.lobby_message.edit(
                embed=embed,
                view=None if remove_view else game.lobby_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.HTTPException):
            self.bot.logger.debug(
                "Could not update LastLetter lobby message", exc_info=True
            )

    @staticmethod
    def _next_player_index(game: LastLetterGame, index: int) -> int:
        return (index + 1) % len(game.players)

    @staticmethod
    def _reset_lastletter_interval(game: LastLetterGame) -> None:
        game.awaiting_letter = True
        game.awaiting_player_id = None
        game.required_letters = None
        game.prefix_length = 1
        game.correct_since_reset = 0
        game.force_increase = False

    @staticmethod
    def _advance_lastletter_prefix(game: LastLetterGame, word: str) -> str:
        """Advance the difficulty ramp and return the next required prefix."""

        if game.prefix_length >= MAX_PREFIX_LENGTH:
            game.correct_since_reset = 0
            game.force_increase = False
        elif game.force_increase:
            game.prefix_length += 1
            game.correct_since_reset = 0
            game.force_increase = False
        elif game.correct_since_reset < 2:
            game.correct_since_reset += 1
        elif random.random() < 0.5:
            game.prefix_length += 1
            game.correct_since_reset = 0
        else:
            game.correct_since_reset += 1
            game.force_increase = True
        return word[-game.prefix_length :].casefold()

    async def _pick_lastletter_prefix(
        self,
        game: LastLetterGame,
        player: LastLetterPlayer,
    ) -> tuple[discord.Message, str | None]:
        letters = tuple(random.sample(ALPHABET, 4))
        view = LastLetterLetterView(game, player, letters)
        game.letter_view = view
        game.awaiting_player_id = player.user_id
        try:
            prompt = await game.channel.send(
                f"{_mention(player.user_id)} Pick a starting letter.",
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
            view.message = prompt
            await view.wait()
            return prompt, view.selected
        finally:
            game.letter_view = None
            game.awaiting_player_id = None
            view.stop()

    async def _guess_lastletter_word(
        self,
        game: LastLetterGame,
        player: LastLetterPlayer,
        letters: str,
    ) -> tuple[discord.Message, discord.Message | None]:
        article = "letter" if len(letters) == 1 else "letters"
        prompt = await game.channel.send(
            f"{_mention(player.user_id)} say a word starting with the {article} "
            f"**{letters.upper()}**.",
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
            ),
        )

        def check(message: discord.Message) -> bool:
            return (
                message.channel.id == game.channel_id
                and message.author.id == player.user_id
                and not message.author.bot
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TURN_TIMEOUT
        attempts = 0
        answer: discord.Message | None = None
        while attempts < MAX_GUESSES:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                candidate = await self.bot.wait_for(
                    "message", timeout=remaining, check=check
                )
            except asyncio.TimeoutError:
                break
            normalized = normalize_lastletter_guess(candidate.content)
            if not normalized.startswith(letters):
                continue
            attempts += 1
            if normalized not in game.used_words and is_valid_lastletter_guess(
                normalized, letters
            ):
                answer = candidate
                break
        return prompt, answer

    async def _lose_lastletter_life(
        self,
        game: LastLetterGame,
        player: LastLetterPlayer,
        eliminated: set[int],
    ) -> None:
        player.lives -= 1
        self._reset_lastletter_interval(game)
        current_index = game.turn_index
        if player.lives <= 0:
            eliminated.add(player.user_id)
            game.players.pop(current_index)
            await game.channel.send(
                f"💀 **{discord.utils.escape_markdown(player.name)}** has lost their "
                "last life.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if game.players:
                game.turn_index %= len(game.players)
            return
        game.turn_index = self._next_player_index(game, current_index)

    async def _run_lastletter(self, game: LastLetterGame) -> None:
        eliminated: set[int] = set()
        winner: LastLetterPlayer | None = None
        try:
            while len(game.players) > 1 and not game.finished:
                game.turn_index %= len(game.players)
                player = game.players[game.turn_index]
                if game.awaiting_letter:
                    prompt, selected = await self._pick_lastletter_prefix(game, player)
                    if selected is None:
                        try:
                            await prompt.add_reaction("💔")
                        except (discord.HTTPException, discord.NotFound):
                            pass
                        await self._lose_lastletter_life(game, player, eliminated)
                        continue
                    try:
                        await prompt.add_reaction("✅")
                    except (discord.HTTPException, discord.NotFound):
                        pass
                    game.required_letters = selected.casefold()
                    game.prefix_length = 1
                    game.awaiting_letter = False
                    game.turn_index = self._next_player_index(game, game.turn_index)
                    continue

                letters = game.required_letters
                if not letters:
                    self._reset_lastletter_interval(game)
                    continue
                prompt, answer = await self._guess_lastletter_word(
                    game, player, letters
                )
                if answer is None:
                    try:
                        await prompt.add_reaction("💔")
                    except (discord.HTTPException, discord.NotFound):
                        pass
                    await self._lose_lastletter_life(game, player, eliminated)
                    continue
                normalized_answer = normalize_lastletter_guess(answer.content)
                game.used_words.add(normalized_answer)
                await add_correct_answer_reaction(answer, prompt)
                game.required_letters = self._advance_lastletter_prefix(
                    game, normalized_answer
                )
                game.turn_index = self._next_player_index(game, game.turn_index)

            if len(game.players) == 1:
                winner = game.players[0]
            await self._finish_lastletter(game, winner=winner)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception(
                "LastLetter game failed in channel %s", game.channel_id
            )
            await self._finish_lastletter(game, winner=None, record_results=False)

    async def _finish_lastletter(
        self,
        game: LastLetterGame,
        *,
        winner: LastLetterPlayer | None,
        record_results: bool = True,
    ) -> None:
        if game.finished:
            return
        game.finished = True
        if game.lobby_view is not None:
            game.lobby_view.stop()
        if game.letter_view is not None:
            game.letter_view.stop()
        if self._lastletter_games.get(game.channel_id) is game:
            self._lastletter_games.pop(game.channel_id, None)
        if winner is None:
            await game.channel.send(
                "LastLetter ended with no winner.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        payout = COINS_PER_OPPONENT * max(len(game.participants) - 1, 0)
        awarded = 0
        if self._currency_tracking_enabled(winner.user_id):
            try:
                awarded = await award_lastletter_winner_coins(
                    self.bot.pool,
                    winner.user_id,
                    payout,
                )
            except Exception:
                self.bot.logger.exception("Failed to award LastLetter winner")
        if record_results:
            for user_id in game.participants:
                try:
                    await record_lastletter_result(
                        self.bot.pool,
                        user_id,
                        won=user_id == winner.user_id,
                        tracking_enabled=self.bot.db_cache.user_game_tracking_enabled(
                            user_id
                        ),
                    )
                except Exception:
                    self.bot.logger.exception(
                        "Failed to record LastLetter result for user %s", user_id
                    )
        await game.channel.send(
            f"🏆 **{discord.utils.escape_markdown(winner.name)}** won LastLetter!\n"
            f"-# {awarded:,} Coins added to their wallet.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_lastletter_stats(
        self,
        ctx: Context,
        user: discord.User,
    ) -> None:
        visible = ctx.bot.db_cache.game_history_visible_to(user.id, ctx.author.id)
        selected = None
        if visible:
            selected = await ctx.bot.pool.fetchrow(
                "SELECT wins, losses FROM lastletter_stats WHERE user_id = $1",
                user.id,
            )
        rows = await ctx.bot.pool.fetch(
            "SELECT user_id, wins, losses FROM lastletter_stats "
            "ORDER BY wins DESC, losses ASC, updated_at ASC, user_id ASC LIMIT 5"
        )
        rows = [
            row
            for row in rows
            if ctx.bot.db_cache.game_history_visible_to(
                int(row["user_id"]), ctx.author.id
            )
        ]
        name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(getattr(user, "name", str(user.id)))
        )
        wins = int(selected["wins"]) if selected is not None else 0
        losses = int(selected["losses"]) if selected is not None else 0
        lines = [
            f"## LastLetter stats for {name}",
            f"**Wins:** {wins:,} · **Losses:** {losses:,}",
            "### Most wins",
        ]
        if rows:
            for index, row in enumerate(rows, start=1):
                listed_user = await get_or_fetch_user(ctx.bot, int(row["user_id"]))
                listed_name = getattr(listed_user, "name", None) or str(row["user_id"])
                safe_name = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(listed_name)
                )
                lines.append(
                    f"**#{index} {safe_name}** · "
                    f"{int(row['wins']):,} wins · {int(row['losses']):,} losses"
                )
        else:
            lines.append("No LastLetter games have been recorded yet.")
        embed = discord.Embed(
            title="LastLetter stats",
            description="\n".join(lines),
            color=ctx.bot.embedcolor,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def _currency_tracking_enabled(self, user_id: int) -> bool:
        checker = getattr(self.bot.db_cache, "user_currency_tracking_enabled", None)
        return bool(checker(user_id)) if callable(checker) else True

    def _stop_lastletter_games(self) -> None:
        for game in getattr(self, "_lastletter_games", {}).values():
            game.finished = True
            if game.lobby_view is not None:
                game.lobby_view.stop()
            if game.letter_view is not None:
                game.letter_view.stop()
            if game.task is not None and game.task is not asyncio.current_task():
                game.task.cancel()
        getattr(self, "_lastletter_games", {}).clear()
