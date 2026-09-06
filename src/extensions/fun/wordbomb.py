"""Word Bomb lobby and turn-based game implementation."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import discord
from discord.ext import commands

from utils.emojis import bomb, red_bomb, skull_bomb

from .minigames import (
    add_correct_answer_reaction,
    add_word_bomb_words,
    choose_word_bomb_fragment,
    is_valid_word_bomb_guess,
    is_valid_word_game_word,
)
from .wordbomb_stats import award_wordbomb_winner_coins, record_wordbomb_result

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


LOBBY_TIMEOUT = 60.0
NORMAL_TURN_TIME = 10.0
RED_TURN_TIME = 6.0
SKULL_TURN_TIME = 3.0
RED_AFTER_SECONDS = 90.0
SKULL_AFTER_SECONDS = 180.0


@dataclass(slots=True)
class WordBombPlayer:
    user_id: int
    name: str
    lives: int = 3


@dataclass(slots=True)
class WordBombGame:
    ctx: Context
    channel: discord.abc.Messageable
    channel_id: int
    guild_id: int | None
    host_id: int
    players: list[WordBombPlayer]
    lobby_message: discord.Message | None = None
    lobby_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    lobby_view: WordBombLobbyView | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    started_at: float = field(default_factory=time.monotonic, repr=False)
    turn_index: int = 0
    started: bool = False
    finished: bool = False
    used_words: set[str] = field(default_factory=set, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _username(user: discord.abc.User) -> str:
    """Use usernames in the lobby instead of mutable display names."""

    return str(getattr(user, "name", None) or getattr(user, "display_name", user))


def _mention(user_id: int) -> str:
    return f"<@{user_id}>"


class WordBombLobbyView(discord.ui.View):
    """Join/start controls for the sixty-second Word Bomb lobby."""

    def __init__(self, cog: WordBombCommands, game: WordBombGame):
        super().__init__(timeout=LOBBY_TIMEOUT)
        self.cog = cog
        self.game = game
        self.join_button = discord.ui.Button(
            label="Join",
            style=discord.ButtonStyle.secondary,
            custom_id=f"wordbomb:join:{game.channel_id}",
        )
        self.start_button = discord.ui.Button(
            label="Start game",
            style=discord.ButtonStyle.primary,
            custom_id=f"wordbomb:start:{game.channel_id}",
        )
        self.join_button.callback = self._join
        self.start_button.callback = self._start
        self.add_item(self.join_button)
        self.add_item(self.start_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.finished or self.game.started:
            await interaction.response.send_message(
                "This Word Bomb lobby has already started.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def _join(self, interaction: discord.Interaction) -> None:
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots cannot join Word Bomb games.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with self.game.lock:
            if self.game.started or self.game.finished:
                await interaction.response.send_message(
                    "This Word Bomb lobby has already started.",
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
                self.game.players.append(
                    WordBombPlayer(interaction.user.id, _username(interaction.user))
                )
                response = f"{interaction.user.mention} joined the Word Bomb lobby."
            else:
                self.game.players.pop(existing)
                response = f"{interaction.user.mention} left the Word Bomb lobby."
            await interaction.response.edit_message(
                embed=self.cog._wordbomb_lobby_embed(self.game),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        # Keep this out of the embed so a join/leave does not create a noisy
        # public message; it is still useful as an ephemeral confirmation.
        del response

    async def _start(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.host_id:
            await interaction.response.send_message(
                "Only the person who opened this lobby can start it early.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with self.game.lock:
            if len(self.game.players) < 2:
                await interaction.response.send_message(
                    "At least two players are required to start Word Bomb.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.defer()
            self.game.lobby_event.set()

    async def on_timeout(self) -> None:
        # The lobby timer owns the transition.  This hook only releases the
        # waiter if Discord stops dispatching interactions for the view.
        self.game.lobby_event.set()


class WordBombCommands:
    """Mixin containing the text Word Bomb command and game engine."""

    bot: Fishie
    _wordbomb_games: dict[int, WordBombGame]

    async def load_wordbomb_custom_words(self) -> None:
        """Load owner-added words into the in-memory candidate index."""

        try:
            rows = await self.bot.pool.fetch("SELECT word FROM wordbomb_custom_words")
        except Exception:
            # The numbered migration may not have been applied yet.  Word
            # Bomb still works with its bundled dictionaries in that case.
            self.bot.logger.debug(
                "Could not load custom Word Bomb words", exc_info=True
            )
            return
        add_word_bomb_words(str(row["word"]) for row in rows)

    @cast(Any, commands.command)(
        name="wordbomb",
        aliases=("wb", "word-bomb"),
    )
    async def wordbomb(self, ctx: Context) -> None:
        """Start a Word Bomb lobby."""

        await self._start_wordbomb(ctx)

    @cast(Any, commands.command)(name="checkword", hidden=True)
    async def checkword(self, ctx: Context, *, word: str) -> None:
        """Check whether a word is accepted by Fishie's word games."""

        normalized = word.strip().casefold()
        accepted = is_valid_word_game_word(normalized)
        display = discord.utils.escape_markdown(
            discord.utils.escape_mentions(word.strip())
        )
        if len(display) > 100:
            display = f"{display[:97]}..."
        status = "accepted" if accepted else "not accepted"
        await ctx.send(
            f"**{display or '(empty)'}** is {status} by the Word Bomb and "
            "LastLetter word lists.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _wordbomb_lobby_embed(self, game: WordBombGame) -> discord.Embed:
        players = "\n".join(
            f"{index}. {discord.utils.escape_markdown(player.name)} · "
            f"{player.lives} lives"
            for index, player in enumerate(game.players, start=1)
        )
        if not players:
            players = "No players have joined yet."
        description = (
            "Join before the timer ends, then spell a word containing the shown "
            "letters when it is your turn. Each player starts with three lives.\n\n"
            f"{bomb} Normal: 10 seconds · 2 letters\n"
            f"{red_bomb} Red bomb: 6 seconds · usually 2 letters\n"
            f"{skull_bomb} Skull bomb: 3 seconds · 3 or 4 letters"
        )
        embed = discord.Embed(
            title="Word Bomb lobby",
            description=description,
            color=getattr(self.bot, "embedcolor", discord.Colour.blurple()),
        )
        embed.add_field(
            name=f"Players ({len(game.players)})", value=players, inline=False
        )
        embed.set_footer(
            text="The game starts automatically in 60 seconds, or when the host starts it."
        )
        return embed

    async def _start_wordbomb(self, ctx: Context) -> None:
        if not await cast(Any, self)._require_currency_tracking(ctx):
            return
        games: dict[int, WordBombGame] = self._wordbomb_games
        channel_id = int(ctx.channel.id)
        active = games.get(channel_id)
        if active is not None and not active.finished:
            await ctx.send(
                "There is already a Word Bomb game or lobby in this channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        game = WordBombGame(
            ctx=ctx,
            channel=ctx.channel,
            channel_id=channel_id,
            guild_id=ctx.guild.id if ctx.guild else None,
            host_id=ctx.author.id,
            players=[WordBombPlayer(ctx.author.id, _username(ctx.author))],
        )
        view = WordBombLobbyView(self, game)
        game.lobby_view = view
        try:
            game.lobby_message = await ctx.send(
                embed=self._wordbomb_lobby_embed(game),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            view.stop()
            raise
        games[channel_id] = game
        game.task = asyncio.create_task(
            self._wordbomb_wait_for_lobby(game),
            name=f"wordbomb-lobby-{channel_id}",
        )

    async def _wordbomb_wait_for_lobby(self, game: WordBombGame) -> None:
        try:
            try:
                await asyncio.wait_for(game.lobby_event.wait(), LOBBY_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            await self._begin_wordbomb(game)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception(
                "Word Bomb lobby failed in channel %s", game.channel_id
            )
            await self._finish_wordbomb(game, winner=None)

    async def _begin_wordbomb(self, game: WordBombGame) -> None:
        async with game.lock:
            if game.started or game.finished:
                return
            if game.lobby_view is not None:
                game.lobby_view.stop()
            if len(game.players) < 2:
                game.finished = True
                await self._edit_wordbomb_lobby(
                    game,
                    "Word Bomb needs at least two players to start.",
                    remove_view=True,
                )
                self._wordbomb_games.pop(game.channel_id, None)
                return
            game.started = True
            # The lobby countdown is not part of the difficulty ramp.
            game.started_at = time.monotonic()
            game.turn_index = random.randrange(len(game.players))
            await self._edit_wordbomb_lobby(
                game,
                f"Word Bomb started with **{len(game.players)}** players.",
                remove_view=True,
            )
        game.task = asyncio.create_task(
            self._run_wordbomb(game),
            name=f"wordbomb-game-{game.channel_id}",
        )

    async def _edit_wordbomb_lobby(
        self,
        game: WordBombGame,
        status: str,
        *,
        remove_view: bool = False,
    ) -> None:
        if game.lobby_message is None:
            return
        embed = self._wordbomb_lobby_embed(game)
        embed.description = f"{status}\n\n{embed.description}"
        try:
            await game.lobby_message.edit(
                embed=embed,
                view=None if remove_view else game.lobby_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.HTTPException):
            self.bot.logger.debug(
                "Could not update Word Bomb lobby message", exc_info=True
            )

    @staticmethod
    def _wordbomb_speed(elapsed: float) -> tuple[str, str, float]:
        if elapsed >= SKULL_AFTER_SECONDS:
            return "skull", str(skull_bomb), SKULL_TURN_TIME
        if elapsed >= RED_AFTER_SECONDS:
            return "red", str(red_bomb), RED_TURN_TIME
        return "normal", str(bomb), NORMAL_TURN_TIME

    @staticmethod
    def _wordbomb_fragment_length(speed: str) -> int:
        if speed == "normal":
            return 2
        if speed == "red":
            return random.choices((2, 3), weights=(70, 30), k=1)[0]
        return random.choices((3, 4, 2), weights=(60, 30, 10), k=1)[0]

    async def _run_wordbomb(self, game: WordBombGame) -> None:
        eliminated: set[int] = set()
        winner: WordBombPlayer | None = None
        try:
            while game.players and not game.finished:
                speed, speed_emoji, turn_time = self._wordbomb_speed(
                    time.monotonic() - game.started_at
                )
                fragment = choose_word_bomb_fragment(
                    self._wordbomb_fragment_length(speed),
                    easy=speed == "normal",
                ).upper()
                solved = False
                attempted_this_cycle: set[int] = set()
                while game.players and not game.finished and not solved:
                    if game.turn_index >= len(game.players):
                        game.turn_index = 0
                    player = game.players[game.turn_index]
                    prompt = await game.channel.send(
                        f"{_mention(player.user_id)} spell a word containing the letters "
                        f"**{fragment}** {speed_emoji}",
                        allowed_mentions=discord.AllowedMentions(
                            users=True, roles=False, everyone=False
                        ),
                    )

                    def check(message: discord.Message) -> bool:
                        return (
                            message.channel.id == game.channel_id
                            and message.author.id == player.user_id
                            and not message.author.bot
                        )

                    # Start the clock after the prompt has been sent. A
                    # player may submit as many guesses as they like during
                    # this window; incorrect guesses do not cost a life.
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + turn_time
                    answer: discord.Message | None = None
                    try:
                        async with game.ctx.typing():
                            while True:
                                remaining = deadline - loop.time()
                                if remaining <= 0:
                                    break
                                try:
                                    candidate = await self.bot.wait_for(
                                        "message", timeout=remaining, check=check
                                    )
                                except asyncio.TimeoutError:
                                    break
                                normalized_candidate = (
                                    candidate.content.strip().casefold()
                                )
                                if (
                                    normalized_candidate not in game.used_words
                                    and is_valid_word_bomb_guess(
                                        normalized_candidate, fragment
                                    )
                                ):
                                    answer = candidate
                                    game.used_words.add(normalized_candidate)
                                    break
                    except asyncio.TimeoutError:
                        # Context typing implementations may surface the
                        # timeout from the context manager itself.
                        pass
                    attempted_this_cycle.add(player.user_id)
                    correct = answer is not None and is_valid_word_bomb_guess(
                        answer.content, fragment
                    )
                    if correct:
                        await add_correct_answer_reaction(answer, prompt)
                        solved = True
                        game.turn_index = (game.turn_index + 1) % len(game.players)
                        continue

                    try:
                        await prompt.add_reaction("💔")
                    except (discord.HTTPException, discord.NotFound):
                        pass

                    player.lives -= 1
                    if player.lives > 0:
                        game.turn_index = (game.turn_index + 1) % len(game.players)
                    else:
                        eliminated.add(player.user_id)
                        await game.channel.send(
                            f"💀 **{discord.utils.escape_markdown(player.name)}** has failed!",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        game.players.pop(game.turn_index)
                        if not game.players:
                            break
                        if len(game.players) == 1:
                            winner = game.players[0]
                            break
                        if game.turn_index >= len(game.players):
                            game.turn_index = 0

                    # Every active player has now had one unsuccessful turn
                    # for this fragment.  Move on to a fresh fragment rather
                    # than making the whole table repeat the same prompt.
                    if all(
                        current.user_id in attempted_this_cycle
                        for current in game.players
                    ):
                        break

                if winner is not None:
                    break

            await self._finish_wordbomb(game, winner=winner, eliminated=eliminated)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception(
                "Word Bomb game failed in channel %s", game.channel_id
            )
            await self._finish_wordbomb(game, winner=None, eliminated=eliminated)

    async def _finish_wordbomb(
        self,
        game: WordBombGame,
        *,
        winner: WordBombPlayer | None,
        eliminated: set[int] | None = None,
    ) -> None:
        if game.finished and self._wordbomb_games.get(game.channel_id) is not game:
            return
        game.finished = True
        if game.lobby_view is not None:
            game.lobby_view.stop()
        self._wordbomb_games.pop(game.channel_id, None)
        eliminated = eliminated or set()
        if winner is not None:
            try:
                await record_wordbomb_result(
                    self.bot.pool,
                    winner.user_id,
                    won=True,
                    tracking_enabled=self.bot.db_cache.user_game_tracking_enabled(
                        winner.user_id
                    ),
                )
                awarded = await award_wordbomb_winner_coins(
                    self.bot.pool, winner.user_id
                )
            except Exception:
                self.bot.logger.exception("Failed to record Word Bomb winner")
                awarded = 0
            await game.channel.send(
                f"🏆 **{discord.utils.escape_markdown(winner.name)}** won Word Bomb!\n"
                f"-# {awarded:,} Coins added to their wallet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await game.channel.send(
                "Word Bomb ended with no players remaining.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        for player in eliminated:
            if winner is not None and player == winner.user_id:
                continue
            try:
                await record_wordbomb_result(
                    self.bot.pool,
                    player,
                    won=False,
                    tracking_enabled=self.bot.db_cache.user_game_tracking_enabled(
                        player
                    ),
                )
            except Exception:
                self.bot.logger.exception("Failed to record Word Bomb loss")

    def _stop_wordbomb_games(self) -> None:
        for game in getattr(self, "_wordbomb_games", {}).values():
            game.finished = True
            if game.lobby_view is not None:
                game.lobby_view.stop()
            if game.task is not None and game.task is not asyncio.current_task():
                game.task.cancel()
        getattr(self, "_wordbomb_games", {}).clear()
