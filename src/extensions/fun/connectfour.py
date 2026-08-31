from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

from core.badges import schedule_stat_badge_refresh
from core.currency import award_daily_capped_coins

from .connectfour_solver import ScoredMove, score_moves
from .pvp import DuelBidView

if TYPE_CHECKING:
    from extensions.context import Context


ROWS = 6
COLS = 7
CONNECT = 4
MOVE_TIMEOUT = 60.0
BOT_WIN_COIN_REWARDS = {
    "easy": (50, 10_000),
    "normal": (200, 10_000),
    "hard": (1_000, 10_000),
}
COLORS = ("yellow", "red")
COLOR_EMOJIS = {
    "empty": "\U000026aa",
    "yellow": "\U0001f7e1",
    "red": "\U0001f534",
}


Board = list[list[str | None]]


def other_color(color: str) -> str:
    """Return the opposing Connect Four color."""

    if color == "yellow":
        return "red"
    if color == "red":
        return "yellow"
    raise ValueError("Unknown Connect Four color.")


def random_starting_color() -> str:
    """Choose either color with equal probability."""

    return COLORS[random.getrandbits(1)]


def random_player_colors(first_player_id: int, second_player_id: int) -> dict[str, int]:
    """Assign Red to a randomly selected first player and Yellow to the other."""

    if random_starting_color() == "red":
        return {"red": first_player_id, "yellow": second_player_id}
    return {"red": second_player_id, "yellow": first_player_id}


def new_board() -> Board:
    return [[None for _ in range(COLS)] for _ in range(ROWS)]


def legal_columns(board: Board) -> list[int]:
    return [column for column in range(COLS) if board[0][column] is None]


def next_available_column(board: Board, current: int, direction: int = 1) -> int | None:
    """Return the next non-full column, wrapping around the board."""

    available = set(legal_columns(board))
    if not available:
        return None
    step = 1 if direction >= 0 else -1
    for offset in range(1, COLS + 1):
        candidate = (current + step * offset) % COLS
        if candidate in available:
            return candidate
    return None


def drop_piece(board: Board, column: int, color: str) -> int:
    """Drop a piece and return its row, raising for an invalid/full column."""

    if color not in COLORS:
        raise ValueError("Unknown Connect Four color.")
    if column < 0 or column >= COLS:
        raise ValueError("That column is outside the board.")
    for row in range(ROWS - 1, -1, -1):
        if board[row][column] is None:
            board[row][column] = color
            return row
    raise ValueError("That column is full.")


def has_won(board: Board, color: str) -> bool:
    """Return whether color has four connected pieces in any direction."""

    directions = ((0, 1), (1, 0), (1, 1), (1, -1))
    for row in range(ROWS):
        for column in range(COLS):
            if board[row][column] != color:
                continue
            for row_step, column_step in directions:
                end_row = row + row_step * (CONNECT - 1)
                end_column = column + column_step * (CONNECT - 1)
                if not (0 <= end_row < ROWS and 0 <= end_column < COLS):
                    continue
                if all(
                    board[row + row_step * offset][column + column_step * offset]
                    == color
                    for offset in range(CONNECT)
                ):
                    return True
    return False


def board_result(board: Board, color: str | None = None) -> str | None:
    """Return a winning color, ``draw``, or ``None`` while play continues."""

    for candidate in COLORS:
        if (color is None or color == candidate) and has_won(board, candidate):
            return candidate
    if not legal_columns(board):
        return "draw"
    return None


def _undo_drop(board: Board, column: int) -> None:
    # Pieces are stacked from the bottom, so the most recent drop is the
    # highest occupied cell in this contiguous column stack.
    for row in range(ROWS):
        if board[row][column] is not None:
            board[row][column] = None
            return


def _select_hard_move(moves: tuple[ScoredMove, ...]) -> int:
    """Apply Fishie's rare blunders without overriding forced tactics."""

    best_move = moves[0]
    if best_move.tactical in {"win", "block"}:
        tactical_best = [
            move.column
            for move in moves
            if move.score == best_move.score and move.tactical == best_move.tactical
        ]
        return random.choice(tactical_best or [best_move.column])

    best_score = best_move.score
    best = [move.column for move in moves if move.score == best_score]
    worst_score = moves[-1].score
    worst = [move.column for move in moves if move.score == worst_score]
    middle = [
        move.column
        for move in moves
        if move.score != best_score and move.score != worst_score
    ]
    if random.randrange(1000) == 0:
        return random.choice(worst)
    if random.randrange(500) == 0 and middle:
        return random.choice(middle)
    return random.choice(best)


def choose_ai_move(board: Board, ai_color: str, difficulty: str) -> int:
    """Choose a legal column for the requested difficulty."""

    columns = legal_columns(board)
    if not columns:
        raise ValueError("There are no legal Connect Four moves.")
    difficulty = difficulty.casefold()
    human_color = "red" if ai_color == "yellow" else "yellow"

    if difficulty == "easy":
        return random.choice(columns)

    # Normal mode handles immediate wins and blocks, then prefers the center.
    winning: list[int] = []
    blocking: list[int] = []
    for column in columns:
        drop_piece(board, column, ai_color)
        if has_won(board, ai_color):
            winning.append(column)
        _undo_drop(board, column)
        drop_piece(board, column, human_color)
        if has_won(board, human_color):
            blocking.append(column)
        _undo_drop(board, column)
    if difficulty == "normal":
        if winning:
            return random.choice(winning)
        if blocking:
            return random.choice(blocking)
        center_options = [column for column in columns if column in (2, 3, 4)]
        return random.choice(center_options or columns)

    return _select_hard_move(score_moves(board, ai_color))


def board_text(game: "ConnectFourGame") -> str:
    if game.result is None and game.selected_column not in legal_columns(game.board):
        next_column = next_available_column(game.board, game.selected_column)
        if next_column is not None:
            game.selected_column = next_column
    # The cursor is rendered in the actual top board row rather than as a
    # seventh display-only row. This keeps the board 6x7 and lets that top row
    # be filled normally when a column has five pieces beneath it.
    top_row = [COLOR_EMOJIS[cell or "empty"] for cell in game.board[0]]
    if game.result is None and game.board[0][game.selected_column] is None:
        top_row[game.selected_column] = COLOR_EMOJIS[game.current_color]
    rows = ["".join(top_row)]
    rows.extend(
        "".join(COLOR_EMOJIS[cell or "empty"] for cell in row) for row in game.board[1:]
    )
    return "\n".join(rows)


@dataclass
class ConnectFourGame:
    controller: "ConnectFourController"
    ctx: Context
    players: dict[str, int]
    names: dict[int, str]
    against_bot: bool
    difficulty: str | None
    guild_id: int | None
    channel_id: int
    started_by_id: int
    board: Board = field(default_factory=new_board)
    current_color: str = "yellow"
    selected_column: int = COLS // 2
    result: str | None = None
    forfeited_color: str | None = None
    move_count: int = 0
    move_history: list[dict[str, int | str]] = field(default_factory=list)
    message: discord.Message | None = None
    view: discord.ui.LayoutView | None = None
    recorded: bool = False
    coin_reward: int = 0
    # The winner's total PvP wager pool, retained for the completed-board
    # summary after the individual wager reservations are settled and cleared.
    pvp_payout: int = 0
    # Reserved player-vs-player wagers. Bot matches keep this empty.
    wager_ids: dict[int, int] = field(default_factory=dict)
    wager_stakes: dict[int, int] = field(default_factory=dict)
    wager_stake: int = 0
    move_timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def record_move(self, *, color: str, player_id: int, column: int, row: int) -> None:
        """Record a placed piece for move-count and fastest-win statistics."""

        self.move_count += 1
        self.move_history.append(
            {
                "color": color,
                "player_id": player_id,
                "column": column,
                "row": row,
            }
        )

    @property
    def player_ids(self) -> tuple[int, int]:
        # Keep the public player order consistent with the board assignment:
        # player 1 (Red) is followed by player 2 (Yellow).
        return self.players["red"], self.players["yellow"]

    @property
    def bot_id(self) -> int | None:
        if not self.against_bot:
            return None
        return next(
            (
                player_id
                for player_id in self.player_ids
                if player_id != self.started_by_id
            ),
            None,
        )

    @property
    def human_id(self) -> int | None:
        if not self.against_bot:
            return None
        return self.started_by_id

    @property
    def bot_color(self) -> str | None:
        bot_id = self.bot_id
        return self.color_for(bot_id) if bot_id is not None else None

    @property
    def human_color(self) -> str | None:
        human_id = self.human_id
        return self.color_for(human_id) if human_id is not None else None

    def color_for(self, user_id: int) -> str | None:
        for color, player_id in self.players.items():
            if player_id == user_id:
                return color
        return None


class ConnectFourBoardView(discord.ui.LayoutView):
    def __init__(self, game: ConnectFourGame):
        super().__init__(timeout=None)
        self.game = game
        self.rematch_confirmed: set[int] = set()
        self.display = discord.ui.TextDisplay(self.content())
        self.footer = discord.ui.TextDisplay(self.footer_content())
        self.left = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
        self.place = discord.ui.Button(
            label="Place", style=discord.ButtonStyle.secondary
        )
        self.right = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.left.callback = self._left
        self.place.callback = self._place
        self.right.callback = self._right
        self.rematch = discord.ui.Button(
            label="Play again", style=discord.ButtonStyle.secondary, disabled=True
        )
        self.rematch.callback = self._play_again
        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(self.left, self.place, self.right),
            discord.ui.Separator(),
            self.footer,
            accent_color=game.controller.bot.embedcolor,
        )
        self.add_item(self.container)
        # Rematches are a message-level action, separate from the game board.
        self.add_item(discord.ui.ActionRow(self.rematch))
        self.refresh()

    def content(self) -> str:
        title = "## Connect Four"
        if self.game.against_bot and self.game.difficulty:
            title += f" · {self.game.difficulty.title()}"
        return f"{title}\n{board_text(self.game)}"

    def footer_content(self) -> str:
        yellow = discord.utils.escape_markdown(
            self.game.names[self.game.players["yellow"]]
        )
        red = discord.utils.escape_markdown(self.game.names[self.game.players["red"]])
        if self.game.result == "draw":
            status = "Draw game."
        elif self.game.result in COLORS:
            winner = discord.utils.escape_markdown(
                self.game.names[self.game.players[self.game.result]]
            )
            if self.game.forfeited_color:
                forfeited = discord.utils.escape_markdown(
                    self.game.names[self.game.players[self.game.forfeited_color]]
                )
                status = f"{forfeited} forfeited after 60 seconds. {winner} wins!"
            else:
                status = f"{winner} wins!"
            if self.game.pvp_payout:
                status = f"{winner} wins {self.game.pvp_payout:,} Coins!"
            if self.game.coin_reward:
                status += f" · Earned {self.game.coin_reward:,} Coins"
            if self.rematch_confirmed:
                confirmed_names = ", ".join(
                    discord.utils.escape_markdown(self.game.names[user_id])
                    for user_id in self.rematch_confirmed
                )
                status += f" · {confirmed_names} agreed to play again"
        else:
            current = discord.utils.escape_markdown(
                self.game.names[self.game.players[self.game.current_color]]
            )
            status = f"{current}'s turn"
        return (
            f"{COLOR_EMOJIS['red']} {red} · "
            f"{COLOR_EMOJIS['yellow']} {yellow} · {status}"
        )

    def refresh(self) -> None:
        self.display.content = self.content()
        self.footer.content = self.footer_content()
        disabled = self.game.result is not None
        self.left.disabled = disabled
        self.place.disabled = disabled
        self.right.disabled = disabled
        self.rematch.disabled = not disabled

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.view is self and interaction.user.id in self.game.player_ids:
            return True
        await interaction.response.send_message(
            "This Connect Four game is no longer active.", ephemeral=True
        )
        return False

    async def _left(self, interaction: discord.Interaction) -> None:
        await self.game.controller.handle_move(interaction, self.game, "left")

    async def _place(self, interaction: discord.Interaction) -> None:
        await self.game.controller.handle_move(interaction, self.game, "place")

    async def _right(self, interaction: discord.Interaction) -> None:
        await self.game.controller.handle_move(interaction, self.game, "right")

    async def _play_again(self, interaction: discord.Interaction) -> None:
        restart = False
        async with self.game.lock:
            if self.game.view is not self or self.game.result is None:
                await interaction.response.send_message(
                    "This Connect Four game is no longer active.", ephemeral=True
                )
                return
            if self.game.against_bot:
                if interaction.user.id != self.game.human_id:
                    bot_id = self.game.bot_id
                    bot_name = (
                        self.game.names.get(bot_id, "Fishie")
                        if bot_id is not None
                        else "Fishie"
                    )
                    await interaction.response.send_message(
                        "Only the player can start another game against "
                        f"{discord.utils.escape_markdown(bot_name)}.",
                        ephemeral=True,
                    )
                    return
                restart = True
            else:
                if interaction.user.id in self.rematch_confirmed:
                    await interaction.response.send_message(
                        "You already agreed to play again.", ephemeral=True
                    )
                    return
                self.rematch_confirmed.add(interaction.user.id)
                restart = self.rematch_confirmed == set(self.game.player_ids)
                if not restart:
                    self.refresh()
                    await interaction.response.edit_message(
                        view=self, allowed_mentions=discord.AllowedMentions.none()
                    )
                    return

        if restart:
            await self.game.controller.restart_game(self.game, interaction)
            self.stop()


class ConnectFourSetupView(discord.ui.LayoutView):
    def __init__(
        self,
        controller: "ConnectFourController",
        ctx: Context,
        opponent: discord.abc.User | None = None,
    ):
        super().__init__(timeout=60)
        self.controller = controller
        self.ctx = ctx
        self.opponent = opponent
        self.message: discord.Message | None = None
        opponent_name = (
            getattr(opponent, "name", None) if opponent is not None else "Fishie"
        ) or "Fishie"
        self.display = discord.ui.TextDisplay(
            "## Connect Four\n"
            f"Choose a difficulty to play against "
            f"{discord.utils.escape_markdown(opponent_name)}."
        )
        buttons: list[discord.ui.Button] = []
        for difficulty in ("easy", "normal", "hard"):
            button = discord.ui.Button(
                label=difficulty.title(), style=discord.ButtonStyle.secondary
            )

            async def callback(
                interaction: discord.Interaction, selected: str = difficulty
            ) -> None:
                await self._select(interaction, selected)

            button.callback = callback
            buttons.append(button)
        self.buttons = buttons
        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(*buttons),
            accent_color=ctx.bot.embedcolor,
        )
        self.add_item(self.container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message(
            "This game setup is not for you.", ephemeral=True
        )
        return False

    async def _select(self, interaction: discord.Interaction, difficulty: str) -> None:
        game = await self.controller.start_bot_game(
            self.ctx,
            difficulty,
            interaction=interaction,
            opponent=self.opponent,
        )
        if game is not None:
            self.stop()

    async def on_timeout(self) -> None:
        for button in self.buttons:
            button.disabled = True
        self.display.content = "## Connect Four\nThis game setup expired."
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass


class ConnectFourChallengeView(discord.ui.LayoutView):
    def __init__(
        self,
        controller: "ConnectFourController",
        ctx: Context,
        opponent: discord.abc.User,
        *,
        challenger_bid: int | None = None,
    ):
        super().__init__(timeout=60)
        self.controller = controller
        self.ctx = ctx
        self.opponent = opponent
        self.challenger_bid = challenger_bid
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(
            f"## Connect Four\n{opponent.mention}, you have been challenged. Do you want to play?"
        )
        self.accept_button = discord.ui.Button(
            label="Accept", style=discord.ButtonStyle.secondary
        )
        self.decline_button = discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.secondary
        )
        self.accept_button.callback = self._accept
        self.decline_button.callback = self._decline
        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(self.accept_button, self.decline_button),
            accent_color=ctx.bot.embedcolor,
        )
        self.add_item(self.container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opponent.id:
            return True
        await interaction.response.send_message(
            "Only the challenged user can answer this invitation.", ephemeral=True
        )
        return False

    async def _accept(self, interaction: discord.Interaction) -> None:
        if self.challenger_bid is not None:

            async def on_ready(
                ready_interaction: discord.Interaction,
                stakes: tuple[int, int],
            ) -> None:
                try:
                    await self.controller.start_user_game(
                        self.ctx,
                        self.opponent,
                        interaction=ready_interaction,
                        stakes=stakes,
                    )
                except Exception as exc:
                    self.controller.bot.logger.exception(
                        "Failed to start wagered Connect Four duel"
                    )
                    if ready_interaction.response.is_done():
                        await ready_interaction.followup.send(
                            f"I couldn't start that wagered game: {exc}",
                            ephemeral=True,
                        )
                    else:
                        await ready_interaction.response.send_message(
                            f"I couldn't start that wagered game: {exc}",
                            ephemeral=True,
                        )

            bid_view = DuelBidView(
                self.ctx,
                self.ctx.author,
                self.opponent,
                game_name="Connect Four",
                challenger_bid=self.challenger_bid,
                on_ready=on_ready,
            )
            bid_view.message = self.message
            await interaction.response.edit_message(
                # Both the challenge and bid prompts use Components V2.  Do
                # not send a content field while updating an existing V2
                # message; Discord rejects it with error 50035.
                view=bid_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.stop()
            return
        game = await self.controller.start_user_game(
            self.ctx, self.opponent, interaction=interaction
        )
        if game is not None:
            self.stop()

    async def _decline(self, interaction: discord.Interaction) -> None:
        self.accept_button.disabled = True
        self.decline_button.disabled = True
        self.display.content = "## Connect Four\nChallenge declined."
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )
        self.stop()

    async def on_timeout(self) -> None:
        self.accept_button.disabled = True
        self.decline_button.disabled = True
        self.display.content = "## Connect Four\nThis challenge expired."
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass


class ConnectFourStatsView(discord.ui.LayoutView):
    def __init__(self, ctx: Context, stats: dict[str, dict[str, str]]):
        super().__init__(timeout=300)
        self.author_id = ctx.author.id
        self.stats = stats
        self.mode = "players"
        self.sort = "wins"
        self.display = discord.ui.TextDisplay(stats["players"]["content"])
        self.players_button = discord.ui.Button(
            label="Vs Players", style=discord.ButtonStyle.secondary
        )
        self.fishie_button = discord.ui.Button(
            label="Vs Fishie", style=discord.ButtonStyle.secondary
        )
        self.sort_button = discord.ui.Button(
            label="Wins", style=discord.ButtonStyle.secondary
        )
        self.players_button.callback = self._players
        self.fishie_button.callback = self._fishie
        self.sort_button.callback = self._toggle_sort
        self.container = discord.ui.Container(
            self.display,
            discord.ui.Separator(),
            discord.ui.ActionRow(
                self.players_button, self.fishie_button, self.sort_button
            ),
            accent_color=ctx.bot.embedcolor,
        )
        self.add_item(self.container)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.players_button.disabled = self.mode == "players"
        self.fishie_button.disabled = self.mode == "fishie"
        self.sort_button.disabled = self.mode == "players"
        self.sort_button.label = self.sort.title()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "This Connect Four leaderboard is not for you.", ephemeral=True
        )
        return False

    async def _players(self, interaction: discord.Interaction) -> None:
        self.mode = "players"
        self.display.content = self.stats["players"]["content"]
        self._refresh_buttons()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _fishie(self, interaction: discord.Interaction) -> None:
        self.mode = "fishie"
        self.display.content = self.stats["fishie"][self.sort]
        self._refresh_buttons()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _toggle_sort(self, interaction: discord.Interaction) -> None:
        self.sort = {"wins": "losses", "losses": "draws", "draws": "wins"}[self.sort]
        self.display.content = self.stats["fishie"][self.sort]
        self._refresh_buttons()
        await interaction.response.edit_message(
            view=self, allowed_mentions=discord.AllowedMentions.none()
        )


class ConnectFourController:
    def __init__(self, cog: Any):
        self.cog = cog
        self.bot = cog.bot
        self.games: dict[int, ConnectFourGame] = {}

    def _active(self, ids: tuple[int, ...]) -> ConnectFourGame | None:
        for user_id in ids:
            game = self.games.get(user_id)
            if game is not None:
                return game
        return None

    def _register(self, game: ConnectFourGame) -> None:
        for user_id in game.player_ids:
            if not game.against_bot or user_id == game.human_id:
                self.games[user_id] = game

    def remove_game(self, game: ConnectFourGame) -> None:
        for user_id in game.player_ids:
            if self.games.get(user_id) is game:
                self.games.pop(user_id, None)

    def close(self) -> None:
        """Stop active games when the Fun cog is reloaded."""

        games = list({id(game): game for game in self.games.values()}.values())
        for game in games:
            self.cancel_timeout(game)
            if game.view is not None:
                game.view.stop()
        self.games.clear()

    def cancel_timeout(self, game: ConnectFourGame) -> None:
        task = game.move_timeout_task
        game.move_timeout_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def schedule_timeout(self, game: ConnectFourGame) -> None:
        self.cancel_timeout(game)
        if game.result is None:
            game.move_timeout_task = asyncio.create_task(self._timeout(game))

    async def award_bot_win(self, game: ConnectFourGame) -> int:
        """Award a capped daily Coin reward for a human win against Fishie."""

        if not game.against_bot or game.result not in COLORS:
            return 0
        winner_id = game.players[game.result]
        if winner_id == game.bot_id:
            return 0
        difficulty = (game.difficulty or "").casefold()
        reward = BOT_WIN_COIN_REWARDS.get(difficulty)
        if reward is None:
            return 0
        amount, daily_cap = reward
        return await award_daily_capped_coins(
            self.bot.pool,
            winner_id,
            amount,
            daily_cap,
            f"game_connectfour_{difficulty}",
        )

    async def _play_bot_turn(self, game: ConnectFourGame) -> None:
        """Play and record Fishie's turn using its assigned color."""

        bot_color = game.bot_color
        bot_id = game.bot_id
        if bot_color is None or bot_id is None or game.current_color != bot_color:
            return
        ai_column = await asyncio.to_thread(
            choose_ai_move,
            game.board,
            bot_color,
            game.difficulty or "normal",
        )
        ai_row = drop_piece(game.board, ai_column, bot_color)
        game.record_move(
            color=bot_color,
            player_id=bot_id,
            column=ai_column,
            row=ai_row,
        )
        game.selected_column = COLS // 2
        game.result = board_result(game.board, bot_color)
        if game.result is None:
            game.current_color = other_color(bot_color)

    async def _timeout(self, game: ConnectFourGame) -> None:
        try:
            await asyncio.sleep(MOVE_TIMEOUT)
            async with game.lock:
                if game.result is not None:
                    return
                game.forfeited_color = game.current_color
                game.result = "red" if game.current_color == "yellow" else "yellow"
                if isinstance(game.view, ConnectFourBoardView):
                    game.view.refresh()
                await self.record_game(game)
                message = game.message
                view = game.view
            if message is not None and isinstance(view, ConnectFourBoardView):
                await message.edit(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.bot.logger.exception("Failed to forfeit inactive Connect Four game")

    async def _send_or_edit(
        self,
        ctx: Context,
        game: ConnectFourGame,
        view: discord.ui.LayoutView,
        interaction: discord.Interaction | None,
    ) -> None:
        if interaction is None:
            game.message = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        if not interaction.response.is_done():
            await interaction.response.edit_message(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            game.message = interaction.message
        elif interaction.message is not None:
            game.message = await interaction.message.edit(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            game.message = await interaction.edit_original_response(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _edit_interaction_view(
        self,
        interaction: discord.Interaction,
        view: discord.ui.LayoutView,
    ) -> discord.Message | None:
        """Edit a component response whether or not it was deferred."""

        if not interaction.response.is_done():
            await interaction.response.edit_message(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            return interaction.message
        if interaction.message is not None:
            return await interaction.message.edit(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        return await interaction.edit_original_response(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def start_bot_game(
        self,
        ctx: Context,
        difficulty: str,
        *,
        interaction: discord.Interaction | None = None,
        opponent: discord.abc.User | None = None,
    ) -> ConnectFourGame | None:
        # Keep Fishie's ID as the logical opponent so game history, rewards,
        # and leaderboards continue to be attributed to Fishie.  A supplied
        # bot is only a display name for the otherwise identical house game.
        bot_id = (
            self.bot.user.id if self.bot.user else int(self.bot.config["ids"]["bot_id"])
        )
        active = self._active((ctx.author.id,))
        if active is not None:
            message = "You already have an active Connect Four game."
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await ctx.send(message)
            return None
        opponent_name = (
            getattr(opponent, "name", None)
            if opponent is not None
            else getattr(self.bot.user, "name", None)
        ) or "Fishie"
        names = {
            ctx.author.id: getattr(ctx.author, "name", str(ctx.author.id)),
            bot_id: opponent_name,
        }
        # Pick the first player fairly, then assign that player Red.  Red is
        # always the opening turn; the player selected as Red is not tied to
        # whoever started the command.
        players = random_player_colors(ctx.author.id, bot_id)
        bot_color = "red" if players["red"] == bot_id else "yellow"
        game = ConnectFourGame(
            self,
            ctx,
            players,
            names,
            True,
            difficulty,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            ctx.author.id,
            current_color="red",
        )
        if game.current_color == bot_color:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.defer()
            await self._play_bot_turn(game)
        self._register(game)
        view = ConnectFourBoardView(game)
        game.view = view
        try:
            await self._send_or_edit(ctx, game, view, interaction)
        except Exception:
            self.remove_game(game)
            raise
        self.schedule_timeout(game)
        return game

    async def start_user_game(
        self,
        ctx: Context,
        opponent: discord.abc.User,
        *,
        interaction: discord.Interaction | None = None,
        stakes: tuple[int, int] | None = None,
    ) -> ConnectFourGame | None:
        active = self._active((ctx.author.id, opponent.id))
        if active is not None:
            message = "One of those users already has an active Connect Four game."
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await ctx.send(message)
            return None
        names = {
            ctx.author.id: getattr(ctx.author, "name", str(ctx.author.id)),
            opponent.id: getattr(opponent, "name", str(opponent.id)),
        }
        player_ids = (ctx.author.id, opponent.id)
        wager_ids: dict[int, int] = {}
        wager_stakes: dict[int, int] = {}
        wager_stake = 0
        if stakes is not None:
            if len(stakes) != 2 or any(int(stake) <= 0 for stake in stakes):
                raise ValueError("Player wagers must be positive.")
            wager_stakes = {
                int(player_ids[0]): int(stakes[0]),
                int(player_ids[1]): int(stakes[1]),
            }
            # Retain the original field for compatibility with older callers;
            # settlement uses the per-player mapping below.
            wager_stake = int(stakes[0])
            checker = getattr(
                getattr(self.bot, "db_cache", None),
                "user_currency_tracking_enabled",
                None,
            )
            if callable(checker) and any(
                not checker(player_id) for player_id in player_ids
            ):
                raise ValueError(
                    "Both players must have currency tracking enabled to place a bid."
                )
            try:
                for player_id, player_stake in wager_stakes.items():
                    wager = await self.bot.currency.open_wager(
                        player_id,
                        player_stake,
                        source="pvp_connectfour",
                    )
                    wager_ids[player_id] = wager.id
            except Exception:
                for player_id, wager_id in wager_ids.items():
                    try:
                        await self.bot.currency.settle_wager(
                            wager_id,
                            wager_stakes[player_id],
                            track_stats=False,
                        )
                    except Exception:
                        self.bot.logger.exception(
                            "Failed to refund a Connect Four duel wager"
                        )
                raise

        game = ConnectFourGame(
            self,
            ctx,
            # Randomly select player 1, who always receives Red and the first
            # turn.  The host is not given a color preference.
            random_player_colors(ctx.author.id, opponent.id),
            names,
            False,
            None,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            ctx.author.id,
            current_color="red",
            wager_ids=wager_ids,
            wager_stakes=wager_stakes,
            wager_stake=wager_stake,
        )
        self._register(game)
        view = ConnectFourBoardView(game)
        game.view = view
        try:
            await self._send_or_edit(ctx, game, view, interaction)
        except Exception:
            self.remove_game(game)
            for player_id, wager_id in wager_ids.items():
                try:
                    await self.bot.currency.settle_wager(
                        wager_id,
                        wager_stakes.get(player_id, wager_stake),
                        track_stats=False,
                    )
                except Exception:
                    self.bot.logger.exception(
                        "Failed to refund Connect Four wagers after startup failed"
                    )
            raise
        self.schedule_timeout(game)
        return game

    async def restart_game(
        self, game: ConnectFourGame, interaction: discord.Interaction
    ) -> None:
        """Start a rematch from a completed game using its original settings."""

        async with game.lock:
            if game.result is None or not isinstance(game.view, ConnectFourBoardView):
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "This Connect Four game is still in progress or expired.",
                        ephemeral=True,
                    )
                return

            active_ids = (
                (game.human_id,)
                if game.against_bot and game.human_id is not None
                else game.player_ids
            )
            active = self._active(active_ids)
            if active is not None and active is not game:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "One of those users already has an active Connect Four game.",
                        ephemeral=True,
                    )
                return

            self.cancel_timeout(game)
            game.board = new_board()
            game.selected_column = COLS // 2
            game.result = None
            game.forfeited_color = None
            game.move_count = 0
            game.move_history.clear()
            game.recorded = False
            game.coin_reward = 0
            game.pvp_payout = 0
            if game.against_bot:
                human_id = game.human_id
                bot_id = game.bot_id
                if human_id is None or bot_id is None:
                    raise RuntimeError("Connect Four bot game has missing players.")
                # Pick a fresh random first player for every rematch and give
                # that player Red, which also remains the opening turn.
                game.players = random_player_colors(human_id, bot_id)
                bot_color = game.color_for(bot_id)
            else:
                # Player-vs-player rematches also get a fresh random player 1.
                game.players = random_player_colors(
                    game.players["red"], game.players["yellow"]
                )

            game.current_color = "red"
            if game.against_bot:
                if game.current_color == bot_color:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                    await self._play_bot_turn(game)
            self._register(game)
            view = ConnectFourBoardView(game)
            game.view = view
            game.message = await self._edit_interaction_view(interaction, view)
            self.schedule_timeout(game)

    async def handle_move(
        self, interaction: discord.Interaction, game: ConnectFourGame, action: str
    ) -> None:
        async with game.lock:
            if game.result is not None or not isinstance(
                game.view, ConnectFourBoardView
            ):
                await interaction.response.send_message(
                    "This Connect Four game is no longer active.", ephemeral=True
                )
                return
            # A previous move can fill the column under the cursor, especially
            # after Fishie's automatic turn. Normalize the cursor before any
            # action so Place never has to guess a different column.
            if game.selected_column not in legal_columns(game.board):
                next_column = next_available_column(game.board, game.selected_column)
                if next_column is None:
                    game.result = "draw"
                    self.cancel_timeout(game)
                    await self.record_game(game)
                    view = game.view
                    view.refresh()
                    await interaction.response.edit_message(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                game.selected_column = next_column
            color = game.color_for(interaction.user.id)
            if color is None or color != game.current_color:
                await interaction.response.send_message(
                    "It is not your turn yet.", ephemeral=True
                )
                return
            if action == "left":
                next_column = next_available_column(
                    game.board, game.selected_column, -1
                )
                if next_column is not None:
                    game.selected_column = next_column
            elif action == "right":
                next_column = next_available_column(game.board, game.selected_column, 1)
                if next_column is not None:
                    game.selected_column = next_column
            elif action == "place":
                try:
                    placed_column = game.selected_column
                    placed_color = color or "yellow"
                    placed_row = drop_piece(game.board, placed_column, placed_color)
                except ValueError:
                    await interaction.response.send_message(
                        "That column is full. Choose another column.", ephemeral=True
                    )
                    return
                game.record_move(
                    color=placed_color,
                    player_id=game.players[placed_color],
                    column=placed_column,
                    row=placed_row,
                )
                # Every placed piece starts the next cursor at the center. The
                # player can move it again before choosing their next column.
                game.selected_column = COLS // 2
                result = board_result(game.board, color)
                if result is not None:
                    game.result = result
                elif not game.against_bot:
                    game.current_color = other_color(color)
                else:
                    bot_color = game.bot_color
                    if bot_color is None:
                        raise RuntimeError("Connect Four bot game has no bot color.")
                    game.current_color = bot_color
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                    await self._play_bot_turn(game)
            else:
                await interaction.response.send_message(
                    "Unknown Connect Four action.", ephemeral=True
                )
                return

            if game.result is not None:
                self.cancel_timeout(game)
                await self.record_game(game)
            else:
                self.schedule_timeout(game)
            view = game.view
            view.refresh()
            game.message = await self._edit_interaction_view(interaction, view)

    async def record_game(self, game: ConnectFourGame) -> None:
        if game.recorded:
            return
        game.recorded = True
        try:
            game.coin_reward = await self.award_bot_win(game)
        except Exception:
            self.bot.logger.exception("Failed to award Connect Four Coins")
        if game.wager_ids:
            stakes = game.wager_stakes or {
                player_id: game.wager_stake for player_id in game.wager_ids
            }
            pool = sum(
                stakes.get(player_id, game.wager_stake) for player_id in game.wager_ids
            )
            try:
                if game.result == "draw":
                    for player_id, wager_id in game.wager_ids.items():
                        await self.bot.currency.settle_wager(
                            wager_id,
                            stakes.get(player_id, game.wager_stake),
                            track_stats=False,
                        )
                elif game.result in COLORS:
                    winner_id = game.players[game.result]
                    for player_id, wager_id in game.wager_ids.items():
                        payout = pool if player_id == winner_id else 0
                        await self.bot.currency.settle_wager(wager_id, payout)
                    game.pvp_payout = pool
            except Exception:
                self.bot.logger.exception("Failed to settle Connect Four player wagers")
            finally:
                game.wager_ids.clear()
                game.wager_stakes.clear()
                game.wager_stake = 0
        # A game row contains both human participants.  Skip persistence when
        # either participant has disabled game tracking so an opted-out user
        # is never written into another player's history.
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
            self.cancel_timeout(game)
            self.remove_game(game)
            return
        winner_id = game.players.get(game.result) if game.result in COLORS else None
        loser_id = None
        if winner_id is not None:
            loser_color = "red" if game.result == "yellow" else "yellow"
            loser_id = game.players[loser_color]
        try:
            await self.bot.pool.execute(
                """
                INSERT INTO connectfour_games
                    (guild_id, channel_id, player_yellow_id, player_red_id,
                     winner_id, loser_id, against_bot, bot_difficulty,
                     started_by_id, started_at, finished_at, move_count,
                     move_history)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now(),$11,$12::jsonb)
                """,
                game.guild_id,
                game.channel_id,
                game.players["yellow"],
                game.players["red"],
                winner_id,
                loser_id,
                game.against_bot,
                game.difficulty,
                game.started_by_id,
                discord.utils.utcnow(),
                game.move_count,
                json.dumps(game.move_history, separators=(",", ":")),
            )
            schedule_stat_badge_refresh(self.bot)
        except Exception:
            game.recorded = False
            self.bot.logger.exception("Failed to record Connect Four game")
        finally:
            self.cancel_timeout(game)
            self.remove_game(game)

    async def _label(self, user_id: int) -> str:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            return discord.utils.escape_markdown(user.name)
        except (discord.HTTPException, discord.NotFound):
            return f"User {user_id}"

    async def send_stats(self, ctx: Context) -> ConnectFourStatsView:
        rows = await self.bot.pool.fetch(
            "SELECT player_yellow_id, player_red_id, winner_id, loser_id, "
            "against_bot, bot_difficulty FROM connectfour_games "
            "WHERE against_bot OR winner_id IS NOT NULL OR loser_id IS NOT NULL"
        )
        viewer_id = ctx.author.id
        rows = [
            row
            for row in rows
            if any(
                self.bot.db_cache.game_history_visible_to(int(owner_id), viewer_id)
                for owner_id in (
                    row["player_yellow_id"],
                    row["player_red_id"],
                )
                if owner_id is not None
            )
        ]
        bot_id = (
            self.bot.user.id if self.bot.user else int(self.bot.config["ids"]["bot_id"])
        )
        player_wins: Counter[int] = Counter()
        player_losses: Counter[int] = Counter()
        fishie: dict[str, dict[str, Counter[int]]] = {
            difficulty: {kind: Counter() for kind in ("wins", "losses", "draws")}
            for difficulty in ("easy", "normal", "hard")
        }
        for row in rows:
            winner = row["winner_id"]
            loser = row["loser_id"]
            if row["against_bot"]:
                difficulty = str(row["bot_difficulty"] or "normal").lower()
                if difficulty not in fishie:
                    continue
                if winner is None and loser is None:
                    human = next(
                        (
                            int(value)
                            for value in (row["player_yellow_id"], row["player_red_id"])
                            if int(value) != bot_id
                        ),
                        None,
                    )
                    if human is not None:
                        fishie[difficulty]["draws"][human] += 1
                elif (
                    int(winner or 0) == bot_id
                    and loser is not None
                    and self.bot.db_cache.game_history_visible_to(int(loser), viewer_id)
                ):
                    fishie[difficulty]["losses"][int(loser)] += 1
                elif winner is not None and self.bot.db_cache.game_history_visible_to(
                    int(winner), viewer_id
                ):
                    fishie[difficulty]["wins"][int(winner)] += 1
                continue
            if winner is not None and self.bot.db_cache.game_history_visible_to(
                int(winner), viewer_id
            ):
                player_wins[int(winner)] += 1
            if loser is not None and self.bot.db_cache.game_history_visible_to(
                int(loser), viewer_id
            ):
                player_losses[int(loser)] += 1
        groups = [
            *player_wins.most_common(5),
            *player_losses.most_common(5),
            *(
                (user_id, count)
                for difficulty in fishie.values()
                for counts in difficulty.values()
                for user_id, count in counts.most_common(5)
            ),
        ]
        user_ids = list(dict.fromkeys(user_id for user_id, _count in groups))
        labels = dict(
            zip(
                user_ids,
                await asyncio.gather(*(self._label(user_id) for user_id in user_ids)),
                strict=True,
            )
        )

        def lines(counter: Counter[int], noun: str) -> str:
            return (
                "\n".join(
                    f"**{index}.** {labels[user_id]} · {count:,} {noun}"
                    for index, (user_id, count) in enumerate(counter.most_common(5), 1)
                )
                or "No completed games."
            )

        players = (
            "## Connect Four · Vs Players\n"
            "### Most wins\n"
            f"{lines(player_wins, 'wins')}\n\n"
            "### Most losses\n"
            f"{lines(player_losses, 'losses')}"
        )
        fishie_stats: dict[str, str] = {}
        for kind in ("wins", "losses", "draws"):
            sections = "\n\n".join(
                f"### {difficulty.title()} {kind}\n{lines(fishie[difficulty][kind], kind)}"
                for difficulty in ("easy", "normal", "hard")
            )
            fishie_stats[kind] = f"## Connect Four · Vs Fishie\n{sections}"
        view = ConnectFourStatsView(
            ctx,
            {"players": {"content": players}, "fishie": fishie_stats},
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        return view
