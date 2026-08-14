from __future__ import annotations

import asyncio
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from extensions.context import Context


MARKS = ("X", "O")
EMPTY_CELL_LABEL = "\U00002800"
MARK_LABELS = {"X": "\U0000274c", "O": "\U00002b55"}
MOVE_TIMEOUT = 60.0
WINNING_LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)


def board_result(board: list[str | None]) -> str | None:
    """Return the winning mark, ``draw``, or ``None`` while a game continues."""

    for first, second, third in WINNING_LINES:
        if board[first] and board[first] == board[second] == board[third]:
            return board[first]
    if all(cell is not None for cell in board):
        return "draw"
    return None


def _minimax(board: list[str | None], ai_mark: str, turn_mark: str) -> int:
    result = board_result(board)
    if result == ai_mark:
        return 1
    if result == "draw":
        return 0
    if result is not None:
        return -1

    scores: list[int] = []
    next_mark = "O" if turn_mark == "X" else "X"
    for index, cell in enumerate(board):
        if cell is not None:
            continue
        board[index] = turn_mark
        scores.append(_minimax(board, ai_mark, next_mark))
        board[index] = None

    if turn_mark == ai_mark:
        return max(scores)
    return min(scores)


def choose_ai_move(board: list[str | None], ai_mark: str, difficulty: str) -> int:
    """Pick an AI move according to the requested difficulty."""

    legal_moves = [index for index, cell in enumerate(board) if cell is None]
    if not legal_moves:
        raise ValueError("There are no legal moves.")

    scored_moves: list[tuple[int, int]] = []
    other_mark = "O" if ai_mark == "X" else "X"
    for index in legal_moves:
        board[index] = ai_mark
        score = _minimax(board, ai_mark, other_mark)
        board[index] = None
        scored_moves.append((index, score))

    best_score = max(score for _, score in scored_moves)
    worst_score = min(score for _, score in scored_moves)
    best_moves = [index for index, score in scored_moves if score == best_score]
    worst_moves = [index for index, score in scored_moves if score == worst_score]

    if difficulty == "easy":
        return random.choice(worst_moves)
    if difficulty == "normal" and random.choice((True, False)):
        return random.choice(legal_moves)
    if difficulty == "hard":
        # Keep hard mode optimal almost all of the time, with rare mistakes:
        # one outcome out of 1,000 is a worst move and two outcomes are a
        # middle-ranked move, giving the latter a 1-in-500 chance.
        middle_moves = [
            index
            for index, score in scored_moves
            if score != best_score and score != worst_score
        ]
        roll = random.randrange(1000)
        if roll == 0:
            return random.choice(worst_moves)
        if roll in (1, 2) and middle_moves:
            return random.choice(middle_moves)
    return random.choice(best_moves)


@dataclass
class TicTacToeGame:
    controller: TicTacToeController
    ctx: Context
    players: dict[str, int]
    names: dict[int, str]
    against_bot: bool
    difficulty: str | None
    guild_id: int | None
    channel_id: int
    started_by_id: int
    started_at: datetime = field(default_factory=discord.utils.utcnow)
    board: list[str | None] = field(default_factory=lambda: [None] * 9)
    current_mark: str = "X"
    result: str | None = None
    forfeited_mark: str | None = None
    message: discord.Message | None = None
    # The current view can be the board or one of the rematch/setup views.
    # Keeping this pointer lets stale view timeouts avoid editing a newer
    # game board after a rematch starts.
    view: discord.ui.View | None = None
    recorded: bool = False
    move_timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def player_ids(self) -> tuple[int, int]:
        return self.players["X"], self.players["O"]

    @property
    def bot_id(self) -> int | None:
        if not self.against_bot:
            return None
        user = self.controller.bot.user
        return (
            user.id
            if user is not None
            else int(self.controller.bot.config["ids"]["bot_id"])
        )

    def mark_for(self, user_id: int) -> str | None:
        for mark, player_id in self.players.items():
            if player_id == user_id:
                return mark
        return None

    def apply_move(self, mark: str, index: int) -> None:
        if self.result is not None:
            raise ValueError("The game is over.")
        if self.current_mark != mark:
            raise ValueError("It is not that player's turn.")
        if self.board[index] is not None:
            raise ValueError("That square is already selected.")

        self.board[index] = mark
        self.result = board_result(self.board)
        if self.result is None:
            self.current_mark = "O" if mark == "X" else "X"

    def reset(self, difficulty: str | None = None) -> None:
        self.board = [None] * 9
        self.current_mark = random.choice(MARKS)
        self.result = None
        self.forfeited_mark = None
        self.difficulty = difficulty if self.against_bot else None
        self.recorded = False
        self.started_at = discord.utils.utcnow()
        self.view = None
        self.move_timeout_task = None


class TicTacToeView(discord.ui.View):
    def __init__(self, game: TicTacToeGame):
        super().__init__(timeout=900)
        self.game = game
        self.cells: list[discord.ui.Button] = []
        for index in range(9):
            button = discord.ui.Button(
                label=EMPTY_CELL_LABEL,
                style=discord.ButtonStyle.secondary,
                row=index // 3,
            )

            async def callback(
                interaction: discord.Interaction, cell_index: int = index
            ) -> None:
                await self._select_cell(interaction, cell_index)

            button.callback = callback
            self.cells.append(button)
            self.add_item(button)

        self.rematch = discord.ui.Button(
            label="Play again",
            style=discord.ButtonStyle.primary,
            row=3,
            disabled=True,
        )
        self.rematch.callback = self._play_again
        self.add_item(self.rematch)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.view is self and interaction.user.id in self.game.player_ids:
            return True
        await interaction.response.send_message(
            "This Tic-Tac-Toe game is no longer active.", ephemeral=True
        )
        return False

    def refresh(self) -> None:
        for index, button in enumerate(self.cells):
            mark = self.game.board[index]
            button.label = EMPTY_CELL_LABEL if mark is None else MARK_LABELS[mark]
            # Keep selected squares enabled during play so their X/O colors
            # remain visible. The callback rejects an already occupied square.
            button.disabled = self.game.result is not None
            if mark == "X":
                button.style = discord.ButtonStyle.green
            elif mark == "O":
                button.style = discord.ButtonStyle.blurple
            else:
                button.style = discord.ButtonStyle.secondary
        self.rematch.disabled = self.game.result is None

    async def _select_cell(self, interaction: discord.Interaction, index: int) -> None:
        if self.game.view is not self:
            await interaction.response.send_message(
                "This Tic-Tac-Toe game is no longer active.", ephemeral=True
            )
            return

        mark = self.game.mark_for(interaction.user.id)
        if mark is None:
            return

        async with self.game.lock:
            if self.game.view is not self:
                await interaction.response.send_message(
                    "This Tic-Tac-Toe game is no longer active.", ephemeral=True
                )
                return
            if self.game.result is not None:
                await interaction.response.send_message(
                    "This game is already over.", ephemeral=True
                )
                return
            if self.game.current_mark != mark:
                await interaction.response.send_message(
                    "It is not your turn yet.", ephemeral=True
                )
                return
            if self.game.board[index] is not None:
                await interaction.response.send_message(
                    "That square is already selected.", ephemeral=True
                )
                return

            await interaction.response.defer()
            self.game.apply_move(mark, index)
            if (
                self.game.result is None
                and self.game.against_bot
                and self.game.current_mark == self.game.mark_for(self.game.bot_id or 0)
            ):
                ai_mark = self.game.current_mark
                ai_index = choose_ai_move(
                    self.game.board, ai_mark, self.game.difficulty or "normal"
                )
                self.game.apply_move(ai_mark, ai_index)

            self.refresh()
            if self.game.result is not None:
                await self.game.controller.record_game(self.game)
            else:
                self.game.controller.schedule_move_timeout(self.game)
            embed = self.game.controller.game_embed(self.game)
            if interaction.message is not None:
                self.game.message = await interaction.message.edit(
                    embed=embed, view=self
                )
            else:
                self.game.message = await interaction.edit_original_response(
                    embed=embed, view=self
                )

    async def _play_again(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.view is not self or self.game.result is None:
                await interaction.response.send_message(
                    "This Tic-Tac-Toe game is no longer active.", ephemeral=True
                )
                return
            if self.game.against_bot:
                view = TicTacToeDifficultyView(self.game.controller, self.game)
            else:
                view = TicTacToeRematchView(self.game.controller, self.game)
            previous_view = self.game.view
            self.game.view = view
            try:
                await interaction.response.edit_message(
                    embed=self.game.controller.rematch_embed(self.game), view=view
                )
            except Exception:
                self.game.view = previous_view
                raise
            view.message = interaction.message
            self.stop()

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                return
            self.game.controller.cancel_move_timeout(self.game)
            self.game.controller.remove_game(self.game)
            self.game.view = None
            for button in self.children:
                if isinstance(button, discord.ui.Button):
                    button.disabled = True
            if self.game.message is not None:
                try:
                    await self.game.message.edit(view=self)
                except discord.HTTPException:
                    pass


class TicTacToeDifficultyView(discord.ui.View):
    def __init__(self, controller: TicTacToeController, game: TicTacToeGame):
        super().__init__(timeout=60)
        self.controller = controller
        self.game = game
        self.message: discord.Message | None = None

        for difficulty in ("easy", "normal", "hard"):
            button = discord.ui.Button(
                label=difficulty.title(), style=discord.ButtonStyle.primary
            )

            async def callback(
                interaction: discord.Interaction, selected: str = difficulty
            ) -> None:
                await self._select_difficulty(interaction, selected)

            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        human_id = self.game.players["X"]
        if self.game.view is self and interaction.user.id == human_id:
            return True
        await interaction.response.send_message(
            "This game setup is no longer active.",
            ephemeral=True,
        )
        return False

    async def _select_difficulty(
        self, interaction: discord.Interaction, difficulty: str
    ) -> None:
        try:
            await self.controller.restart_game(self.game, difficulty, interaction)
        except Exception:
            self.controller.bot.logger.exception(
                "Failed to start Tic-Tac-Toe after difficulty selection"
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    "I couldn't start that game. Please try `fish ttt` again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "I couldn't start that game. Please try `fish ttt` again.",
                    ephemeral=True,
                )
            return
        self.stop()

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                return
            self.controller.remove_game(self.game)
            self.game.view = None
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass


class TicTacToeRematchView(discord.ui.View):
    def __init__(self, controller: TicTacToeController, game: TicTacToeGame):
        super().__init__(timeout=60)
        self.controller = controller
        self.game = game
        self.confirmed: set[int] = set()
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.view is self and interaction.user.id in self.game.player_ids:
            return True
        await interaction.response.send_message(
            "This rematch is no longer active.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm rematch", style=discord.ButtonStyle.green)
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                await interaction.response.send_message(
                    "This rematch is no longer active.", ephemeral=True
                )
                return
            self.confirmed.add(interaction.user.id)
            confirmed = self.confirmed == set(self.game.player_ids)
            if not confirmed:
                await interaction.response.edit_message(
                    embed=self.controller.rematch_embed(
                        self.game,
                        f"{self.controller.player_name(interaction.user.id)} confirmed. "
                        "Waiting for the other player.",
                    ),
                    view=self,
                )

        if confirmed:
            await self.controller.restart_game(self.game, None, interaction)
            self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                await interaction.response.send_message(
                    "This rematch is no longer active.", ephemeral=True
                )
                return
            self.controller.remove_game(self.game)
            self.game.view = None
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            self.stop()
            await interaction.response.edit_message(
                embed=self.controller.rematch_embed(self.game, "Rematch declined."),
                view=self,
            )

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                return
            self.controller.remove_game(self.game)
            self.game.view = None
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass


class TicTacToeChallengeView(discord.ui.View):
    """Ask the challenged user to accept a player-versus-player game."""

    def __init__(
        self,
        controller: TicTacToeController,
        ctx: Context,
        opponent: discord.abc.User,
    ):
        super().__init__(timeout=60)
        self.controller = controller
        self.ctx = ctx
        self.opponent = opponent
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opponent.id:
            return True
        await interaction.response.send_message(
            "Only the challenged user can answer this invitation.", ephemeral=True
        )
        return False

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        try:
            game = await self.controller.start_user_game(
                self.ctx, self.opponent, interaction=interaction
            )
        except Exception:
            self.controller.bot.logger.exception(
                "Failed to start Tic-Tac-Toe after challenge acceptance"
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    "I couldn't start that game. Please try again.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "I couldn't start that game. Please try again.", ephemeral=True
                )
            return

        if game is not None:
            self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.disable_all()
        self.stop()
        await interaction.response.edit_message(
            content="Tic-Tac-Toe challenge declined.", view=self
        )

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Tic-Tac-Toe challenge expired.", view=self
                )
            except discord.HTTPException:
                pass


class TicTacToeModeView(discord.ui.View):
    def __init__(self, controller: TicTacToeController, ctx: Context):
        super().__init__(timeout=60)
        self.controller = controller
        self.ctx = ctx
        self.message: discord.Message | None = None
        self.selecting = False

        for difficulty in ("easy", "normal", "hard"):
            button = discord.ui.Button(
                label=difficulty.title(), style=discord.ButtonStyle.primary
            )

            async def callback(
                interaction: discord.Interaction, selected: str = difficulty
            ) -> None:
                self.selecting = True
                try:
                    game = await self.controller.start_bot_game(
                        self.ctx, selected, interaction=interaction
                    )
                except Exception:
                    self.selecting = False
                    raise
                if game is not None:
                    self.stop()
                else:
                    self.selecting = False

            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "This game setup is not for you.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        if self.selecting:
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class TicTacToeStatsView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        embeds: dict[str, discord.Embed],
        fishie_embeds: dict[str, discord.Embed] | None = None,
    ):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.embeds = embeds
        self.fishie_embeds = fishie_embeds or {
            "wins": embeds["fishie"],
            "losses": embeds["fishie"],
            "draws": embeds["fishie"],
        }
        self.current_mode = "players"
        self.fishie_sort = "wins"
        self.message: discord.Message | None = None
        self.buttons: dict[str, discord.ui.Button] = {}
        self.fishie_sort_button = discord.ui.Button(
            label="Wins",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.fishie_sort_button.callback = self._toggle_fishie_sort

        for mode, label in (("players", "Vs Players"), ("fishie", "Vs Fishie")):
            button = discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.primary
                    if mode == self.current_mode
                    else discord.ButtonStyle.secondary
                ),
            )

            async def callback(
                interaction: discord.Interaction, selected: str = mode
            ) -> None:
                await self._select_mode(interaction, selected)

            button.callback = callback
            self.buttons[mode] = button
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "This Tic-Tac-Toe leaderboard is not for you.", ephemeral=True
        )
        return False

    async def _select_mode(self, interaction: discord.Interaction, mode: str) -> None:
        self.current_mode = mode
        for button_mode, button in self.buttons.items():
            button.style = (
                discord.ButtonStyle.primary
                if button_mode == mode
                else discord.ButtonStyle.secondary
            )
        if mode == "fishie":
            if self.fishie_sort_button not in self.children:
                self.add_item(self.fishie_sort_button)
            embed = self.fishie_embeds[self.fishie_sort]
        else:
            if self.fishie_sort_button in self.children:
                self.remove_item(self.fishie_sort_button)
            embed = self.embeds[mode]
        await interaction.response.edit_message(
            embed=embed,
            view=self,
        )

    async def _toggle_fishie_sort(self, interaction: discord.Interaction) -> None:
        if self.current_mode != "fishie":
            await interaction.response.send_message(
                "The Fishie sorting option is not active.", ephemeral=True
            )
            return
        self.fishie_sort = {
            "wins": "losses",
            "losses": "draws",
            "draws": "wins",
        }[self.fishie_sort]
        self.fishie_sort_button.label = self.fishie_sort.title()
        await interaction.response.edit_message(
            embed=self.fishie_embeds[self.fishie_sort], view=self
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class TicTacToeController:
    def __init__(self, cog: Any):
        self.cog = cog
        self.bot = cog.bot
        self.games: dict[int, TicTacToeGame] = {}

    def player_name(self, user_id: int) -> str:
        for game in self.games.values():
            if user_id in game.names:
                return game.names[user_id]
        user = self.bot.get_user(user_id)
        return getattr(user, "display_name", getattr(user, "name", str(user_id)))

    @staticmethod
    def _tracked_player_ids(game: TicTacToeGame) -> tuple[int, ...]:
        # Fishie is shared by every bot game, so only track the human player
        # for bot matches. PvP games track both participants.
        return (game.players["X"],) if game.against_bot else game.player_ids

    def _active_game(
        self, player_ids: tuple[int, ...], *, against_bot: bool = False
    ) -> TicTacToeGame | None:
        ids = player_ids[:1] if against_bot else player_ids
        for player_id in ids:
            game = self.games.get(player_id)
            if game is not None:
                return game
        return None

    def _register_game(self, game: TicTacToeGame) -> None:
        for player_id in self._tracked_player_ids(game):
            self.games[player_id] = game

    def remove_game(self, game: TicTacToeGame) -> None:
        for player_id in self._tracked_player_ids(game):
            if self.games.get(player_id) is game:
                self.games.pop(player_id, None)

    def cancel_move_timeout(self, game: TicTacToeGame) -> None:
        task = game.move_timeout_task
        game.move_timeout_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def schedule_move_timeout(self, game: TicTacToeGame) -> None:
        self.cancel_move_timeout(game)
        if game.result is None:
            game.move_timeout_task = asyncio.create_task(
                self._forfeit_after_timeout(game)
            )

    async def _forfeit_after_timeout(self, game: TicTacToeGame) -> None:
        try:
            await asyncio.sleep(MOVE_TIMEOUT)
            async with game.lock:
                if game.result is not None:
                    return

                game.forfeited_mark = game.current_mark
                game.result = "O" if game.current_mark == "X" else "X"
                if isinstance(game.view, TicTacToeView):
                    game.view.refresh()
                await self.record_game(game)
                embed = self.game_embed(game)
                view = game.view

            if game.message is not None and isinstance(view, TicTacToeView):
                await game.message.edit(embed=embed, view=view)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Failed to forfeit inactive Tic-Tac-Toe game")

    async def start_bot_game(
        self,
        ctx: Context,
        difficulty: str,
        *,
        interaction: discord.Interaction | None = None,
    ) -> TicTacToeGame | None:
        bot_id = (
            self.bot.user.id if self.bot.user else int(self.bot.config["ids"]["bot_id"])
        )
        player_ids = (ctx.author.id, bot_id)
        active = self._active_game(player_ids, against_bot=True)
        # A failed component edit can leave an in-memory game registered
        # without a message. Treat that as a stale setup so the next click or
        # command can recover instead of reporting a phantom active game.
        if active is not None and active.message is None:
            self.remove_game(active)
            active = None
        if active is not None:
            message = "You already have an active Tic-Tac-Toe game."
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await ctx.send(message)
            return None

        names = {
            ctx.author.id: getattr(ctx.author, "display_name", ctx.author.name),
            bot_id: getattr(self.bot.user, "display_name", "Fishie"),
        }
        game = TicTacToeGame(
            controller=self,
            ctx=ctx,
            players={"X": ctx.author.id, "O": bot_id},
            names=names,
            against_bot=True,
            difficulty=difficulty,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
            started_by_id=ctx.author.id,
            current_mark=random.choice(MARKS),
        )
        self._register_game(game)
        view = TicTacToeView(game)
        game.view = view

        if game.current_mark == "O":
            ai_index = choose_ai_move(game.board, "O", difficulty)
            game.apply_move("O", ai_index)
            view.refresh()

        embed = self.game_embed(game)
        if interaction is None:
            try:
                game.message = await ctx.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                self.remove_game(game)
                raise
        else:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                if interaction.message is not None:
                    game.message = await interaction.message.edit(
                        content=None,
                        embed=embed,
                        view=view,
                    )
                else:
                    game.message = await interaction.edit_original_response(
                        content=None, embed=embed, view=view
                    )
            except Exception:
                self.remove_game(game)
                raise
        self.schedule_move_timeout(game)
        return game

    async def start_user_game(
        self,
        ctx: Context,
        opponent: discord.abc.User,
        *,
        interaction: discord.Interaction | None = None,
    ) -> TicTacToeGame | None:
        player_ids = (ctx.author.id, opponent.id)
        active = self._active_game(player_ids)
        if active is not None:
            message = "One of those users already has an active Tic-Tac-Toe game."
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await ctx.send(message)
            return None

        names = {
            ctx.author.id: getattr(ctx.author, "display_name", ctx.author.name),
            opponent.id: getattr(opponent, "display_name", opponent.name),
        }
        game = TicTacToeGame(
            controller=self,
            ctx=ctx,
            players={"X": ctx.author.id, "O": opponent.id},
            names=names,
            against_bot=False,
            difficulty=None,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
            started_by_id=ctx.author.id,
            current_mark=random.choice(MARKS),
        )
        self._register_game(game)
        view = TicTacToeView(game)
        game.view = view
        embed = self.game_embed(game)
        try:
            if interaction is None:
                game.message = await ctx.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                if interaction.message is not None:
                    game.message = await interaction.message.edit(
                        content=None,
                        embed=embed,
                        view=view,
                    )
                else:
                    game.message = await interaction.edit_original_response(
                        content=None,
                        embed=embed,
                        view=view,
                    )
        except Exception:
            self.remove_game(game)
            raise
        self.schedule_move_timeout(game)
        return game

    async def restart_game(
        self,
        game: TicTacToeGame,
        difficulty: str | None,
        interaction: discord.Interaction,
    ) -> None:
        async with game.lock:
            if game.view is None or isinstance(game.view, TicTacToeView):
                if not interaction.response.is_done():
                    await interaction.response.edit_message(
                        embed=self.rematch_embed(game, "This game setup expired."),
                        view=None,
                    )
                return

            active = self._active_game(game.player_ids, against_bot=game.against_bot)
            if active is not None and active is not game:
                await interaction.response.edit_message(
                    embed=self.rematch_embed(
                        game, "You already have another active Tic-Tac-Toe game."
                    ),
                    view=None,
                )
                return

            self.cancel_move_timeout(game)
            game.reset(difficulty)
            self._register_game(game)
            view = TicTacToeView(game)
            game.view = view
            if game.against_bot and game.current_mark == "O":
                ai_index = choose_ai_move(game.board, "O", game.difficulty or "normal")
                game.apply_move("O", ai_index)
                view.refresh()

            embed = self.game_embed(game)
            if interaction.response.is_done():
                try:
                    game.message = await interaction.edit_original_response(
                        embed=embed, view=view
                    )
                except Exception:
                    self.remove_game(game)
                    raise
            else:
                try:
                    await interaction.response.edit_message(embed=embed, view=view)
                    game.message = interaction.message
                except Exception:
                    self.remove_game(game)
                    raise
            self.schedule_move_timeout(game)

    async def record_game(self, game: TicTacToeGame) -> None:
        if game.recorded:
            return
        game.recorded = True
        # Game tracking can be disabled independently for either human
        # participant.  Do not persist a row containing someone who has opted
        # out, while still cleaning up the in-memory game below.
        bot_id = (
            self.bot.user.id
            if self.bot.user is not None
            else int(self.bot.config["ids"]["bot_id"])
        )
        human_ids = {
            int(player_id)
            for player_id in game.players.values()
            if int(player_id) != bot_id
        }
        if any(
            not self.bot.db_cache.user_game_tracking_enabled(player_id)
            for player_id in human_ids
        ):
            self.cancel_move_timeout(game)
            self.remove_game(game)
            return
        winner_id: int | None = None
        loser_id: int | None = None
        if game.result in MARKS:
            winner_id = game.players[game.result]
            loser_id = game.players["O" if game.result == "X" else "X"]

        try:
            await self.bot.pool.execute(
                """
                INSERT INTO tictactoe_games
                    (guild_id, channel_id, player_x_id, player_o_id,
                     winner_id, loser_id, against_bot, bot_difficulty,
                     started_by_id, started_at, finished_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
                """,
                game.guild_id,
                game.channel_id,
                game.players["X"],
                game.players["O"],
                winner_id,
                loser_id,
                game.against_bot,
                game.difficulty,
                game.started_by_id,
                game.started_at,
            )
        except Exception:
            game.recorded = False
            self.bot.logger.exception("Failed to record Tic-Tac-Toe game")
        finally:
            self.cancel_move_timeout(game)
            self.remove_game(game)

    async def _user_label(self, user_id: int) -> str:
        try:
            user = self.bot.get_user(user_id)
            if user is None:
                user = await self.bot.fetch_user(user_id)
            return discord.utils.escape_markdown(user.name)
        except (discord.HTTPException, discord.NotFound):
            return f"User {user_id}"

    @staticmethod
    def _ranked_lines(
        entries: list[tuple[int, int]],
        names: dict[int, str],
        noun: str,
    ) -> str:
        if not entries:
            return "No completed games recorded."
        return "\n".join(
            f"**{position}.** {names[user_id]} ({count:,} {noun})"
            for position, (user_id, count) in enumerate(entries[:5], start=1)
        )

    async def send_stats(self, ctx: Context) -> TicTacToeStatsView:
        """Fetch and cache all leaderboard embeds for one command response."""

        rows = await self.bot.pool.fetch("""
            SELECT player_x_id, player_o_id, winner_id, loser_id,
                   against_bot, bot_difficulty
            FROM tictactoe_games
            WHERE against_bot OR winner_id IS NOT NULL OR loser_id IS NOT NULL
            """)
        viewer_id = ctx.author.id
        rows = [
            row
            for row in rows
            if any(
                self.bot.db_cache.game_history_visible_to(int(owner_id), viewer_id)
                for owner_id in (
                    row["player_x_id"],
                    row["player_o_id"],
                )
                if owner_id is not None
            )
        ]
        player_wins: Counter[int] = Counter()
        player_losses: Counter[int] = Counter()
        fishie_wins: defaultdict[str, Counter[int]] = defaultdict(Counter)
        fishie_losses: defaultdict[str, Counter[int]] = defaultdict(Counter)
        fishie_draws: defaultdict[str, Counter[int]] = defaultdict(Counter)
        bot_id = (
            self.bot.user.id
            if self.bot.user is not None
            else int(self.bot.config["ids"]["bot_id"])
        )

        for row in rows:
            winner_id = row["winner_id"]
            loser_id = row["loser_id"]
            if bool(row["against_bot"]):
                difficulty = str(row["bot_difficulty"] or "normal").lower()
                if difficulty in {"easy", "normal", "hard"}:
                    if winner_id is None and loser_id is None:
                        player_ids = (
                            int(row["player_x_id"]),
                            int(row["player_o_id"]),
                        )
                        human_id = next(
                            (
                                player_id
                                for player_id in player_ids
                                if player_id != bot_id
                            ),
                            None,
                        )
                        if human_id is not None:
                            fishie_draws[difficulty][human_id] += 1
                    elif winner_id is not None and int(winner_id) == bot_id:
                        if (
                            loser_id is not None
                            and self.bot.db_cache.game_history_visible_to(
                                int(loser_id), viewer_id
                            )
                        ):
                            fishie_losses[difficulty][int(loser_id)] += 1
                    elif (
                        winner_id is not None
                        and self.bot.db_cache.game_history_visible_to(
                            int(winner_id), viewer_id
                        )
                    ):
                        fishie_wins[difficulty][int(winner_id)] += 1
                continue

            if winner_id is None:
                continue
            winner_id = int(winner_id)
            if self.bot.db_cache.game_history_visible_to(winner_id, viewer_id):
                player_wins[winner_id] += 1
            if loser_id is not None and self.bot.db_cache.game_history_visible_to(
                int(loser_id), viewer_id
            ):
                player_losses[int(loser_id)] += 1

        ranked_groups = [
            list(player_wins.most_common()),
            list(player_losses.most_common()),
            list(fishie_wins["easy"].most_common()),
            list(fishie_wins["normal"].most_common()),
            list(fishie_wins["hard"].most_common()),
            list(fishie_losses["easy"].most_common()),
            list(fishie_losses["normal"].most_common()),
            list(fishie_losses["hard"].most_common()),
            list(fishie_draws["easy"].most_common()),
            list(fishie_draws["normal"].most_common()),
            list(fishie_draws["hard"].most_common()),
        ]
        user_ids = {user_id for group in ranked_groups for user_id, _count in group[:5]}
        labels = await asyncio.gather(
            *(self._user_label(user_id) for user_id in user_ids)
        )
        names = dict(zip(user_ids, labels, strict=True))

        players_embed = discord.Embed(
            title="Tic-Tac-Toe Global Stats",
            description="Leaderboard for games against other players.",
            color=getattr(self.bot, "embedcolor", discord.Color.blurple()),
        )
        players_embed.add_field(
            name="Most wins",
            value=self._ranked_lines(
                ranked_groups[0], names, "win" if player_wins else "wins"
            ),
            inline=True,
        )
        players_embed.add_field(
            name="Most losses",
            value=self._ranked_lines(
                ranked_groups[1], names, "loss" if player_losses else "losses"
            ),
            inline=True,
        )

        fishie_wins_embed = discord.Embed(
            title="Tic-Tac-Toe Global Stats",
            description="Leaderboard for games won against Fishie.",
            color=getattr(self.bot, "embedcolor", discord.Color.blurple()),
        )
        fishie_losses_embed = discord.Embed(
            title="Tic-Tac-Toe Global Stats",
            description="Leaderboard for games lost against Fishie.",
            color=getattr(self.bot, "embedcolor", discord.Color.blurple()),
        )
        fishie_draws_embed = discord.Embed(
            title="Tic-Tac-Toe Global Stats",
            description="Leaderboard for drawn games against Fishie.",
            color=getattr(self.bot, "embedcolor", discord.Color.blurple()),
        )
        for difficulty, wins_index, losses_index, draws_index in (
            ("easy", 2, 5, 8),
            ("normal", 3, 6, 9),
            ("hard", 4, 7, 10),
        ):
            fishie_wins_embed.add_field(
                name=f"{difficulty.title()} wins",
                value=self._ranked_lines(ranked_groups[wins_index], names, "win"),
                inline=True,
            )
            fishie_losses_embed.add_field(
                name=f"{difficulty.title()} losses",
                value=self._ranked_lines(ranked_groups[losses_index], names, "loss"),
                inline=True,
            )
            fishie_draws_embed.add_field(
                name=f"{difficulty.title()} draws",
                value=self._ranked_lines(ranked_groups[draws_index], names, "draw"),
                inline=True,
            )

        view = TicTacToeStatsView(
            ctx.author.id,
            {"players": players_embed, "fishie": fishie_wins_embed},
            {
                "wins": fishie_wins_embed,
                "losses": fishie_losses_embed,
                "draws": fishie_draws_embed,
            },
        )
        view.message = await ctx.send(embed=players_embed, view=view)
        return view

    def game_embed(self, game: TicTacToeGame) -> discord.Embed:
        player_x = discord.utils.escape_markdown(game.names[game.players["X"]])
        player_o = discord.utils.escape_markdown(game.names[game.players["O"]])
        if game.result == "draw":
            status = "Draw game."
        elif game.result in MARKS:
            winner = discord.utils.escape_markdown(
                game.names[game.players[game.result]]
            )
            if game.forfeited_mark is not None:
                forfeited = discord.utils.escape_markdown(
                    game.names[game.players[game.forfeited_mark]]
                )
                status = f"{forfeited} forfeited after 60 seconds. {winner} wins!"
            else:
                status = f"{winner} wins!"
        else:
            current = discord.utils.escape_markdown(
                game.names[game.players[game.current_mark]]
            )
            status = f"{current}'s turn ({game.current_mark})"

        title = "Tic-Tac-Toe"
        if game.against_bot and game.difficulty:
            title += f" • {game.difficulty.title()}"
        return discord.Embed(
            title=title,
            description=(
                f"{MARK_LABELS['X']} {player_x}\n"
                f"{MARK_LABELS['O']} {player_o}\n\n{status}"
            ),
            color=getattr(self.bot, "embedcolor", discord.Color.blurple()),
        )

    def rematch_embed(
        self, game: TicTacToeGame, message: str | None = None
    ) -> discord.Embed:
        description = message or (
            "Choose a difficulty to play Fishie again."
            if game.against_bot
            else "Both players must confirm before the rematch starts."
        )
        return discord.Embed(
            title="Tic-Tac-Toe rematch",
            description=description,
            color=getattr(self.bot, "embedcolor", discord.Color.blurple()),
        )
