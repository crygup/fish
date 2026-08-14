from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from extensions.context import Context


ROWS = 6
COLS = 7
CONNECT = 4
MOVE_TIMEOUT = 60.0
COLORS = ("yellow", "red")
COLOR_EMOJIS = {
    "empty": "\U000026aa",
    "yellow": "\U0001f7e1",
    "red": "\U0001f534",
}


Board = list[list[str | None]]


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


def _window_score(window: list[str | None], ai_color: str, human_color: str) -> int:
    ai_count = window.count(ai_color)
    human_count = window.count(human_color)
    empty_count = window.count(None)
    if ai_count == CONNECT:
        return 100_000
    if ai_count == 3 and empty_count == 1:
        return 100
    if ai_count == 2 and empty_count == 2:
        return 10
    if human_count == 3 and empty_count == 1:
        return -120
    if human_count == 2 and empty_count == 2:
        return -8
    return 0


def _heuristic(board: Board, ai_color: str, human_color: str) -> int:
    score = 0
    center = [board[row][COLS // 2] for row in range(ROWS)]
    score += center.count(ai_color) * 6
    score -= center.count(human_color) * 4

    for row in range(ROWS):
        for column in range(COLS - 3):
            score += _window_score(
                board[row][column : column + CONNECT], ai_color, human_color
            )
    for row in range(ROWS - 3):
        for column in range(COLS):
            score += _window_score(
                [board[row + offset][column] for offset in range(CONNECT)],
                ai_color,
                human_color,
            )
    for row in range(ROWS - 3):
        for column in range(COLS - 3):
            score += _window_score(
                [board[row + offset][column + offset] for offset in range(CONNECT)],
                ai_color,
                human_color,
            )
    for row in range(ROWS - 3):
        for column in range(3, COLS):
            score += _window_score(
                [board[row + offset][column - offset] for offset in range(CONNECT)],
                ai_color,
                human_color,
            )
    return score


def _minimax(
    board: Board,
    depth: int,
    maximizing: bool,
    ai_color: str,
    human_color: str,
    alpha: int,
    beta: int,
) -> int:
    result = board_result(board)
    if result == ai_color:
        return 1_000_000 + depth
    if result == human_color:
        return -1_000_000 - depth
    if result == "draw" or depth == 0:
        return _heuristic(board, ai_color, human_color)

    columns = sorted(legal_columns(board), key=lambda value: abs(value - COLS // 2))
    if maximizing:
        value = -(10**9)
        for column in columns:
            drop_piece(board, column, ai_color)
            value = max(
                value,
                _minimax(board, depth - 1, False, ai_color, human_color, alpha, beta),
            )
            _undo_drop(board, column)
            alpha = max(alpha, value)
            if alpha >= beta:
                break
        return value

    value = 10**9
    for column in columns:
        drop_piece(board, column, human_color)
        value = min(
            value,
            _minimax(board, depth - 1, True, ai_color, human_color, alpha, beta),
        )
        _undo_drop(board, column)
        beta = min(beta, value)
        if alpha >= beta:
            break
    return value


def _undo_drop(board: Board, column: int) -> None:
    # Pieces are stacked from the bottom, so the most recent drop is the
    # highest occupied cell in this contiguous column stack.
    for row in range(ROWS):
        if board[row][column] is not None:
            board[row][column] = None
            return


def choose_ai_move(board: Board, ai_color: str, difficulty: str) -> int:
    """Choose a legal column, with hard mode using a shallow alpha-beta search."""

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

    scored: list[tuple[int, int]] = []
    for column in columns:
        drop_piece(board, column, ai_color)
        score = _minimax(board, 4, False, ai_color, human_color, -(10**9), 10**9)
        _undo_drop(board, column)
        scored.append((score, column))
    best_score = max(score for score, _column in scored)
    worst_score = min(score for score, _column in scored)
    best = [column for score, column in scored if score == best_score]
    worst = [column for score, column in scored if score == worst_score]
    middle = [
        column
        for score, column in scored
        if score != best_score and score != worst_score
    ]
    # Match Tic-Tac-Toe's hard-mode personality: an extremely rare bad move
    # and a 1-in-500 middle move, while otherwise selecting the best result.
    if random.randrange(1000) == 0:
        return random.choice(worst)
    if random.randrange(500) == 0 and middle:
        return random.choice(middle)
    return random.choice(best)


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
        return self.players["yellow"], self.players["red"]

    @property
    def bot_id(self) -> int | None:
        if not self.against_bot:
            return None
        return self.players["red"]

    def color_for(self, user_id: int) -> str | None:
        for color, player_id in self.players.items():
            if player_id == user_id:
                return color
        return None


class ConnectFourBoardView(discord.ui.LayoutView):
    def __init__(self, game: ConnectFourGame):
        super().__init__(timeout=None)
        self.game = game
        self.display = discord.ui.TextDisplay(self.content())
        self.footer = discord.ui.TextDisplay(self.footer_content())
        self.rematch_confirmed: set[int] = set()
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
            f"{COLOR_EMOJIS['yellow']} {yellow} · "
            f"{COLOR_EMOJIS['red']} {red} · {status}"
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
                if interaction.user.id != self.game.players["yellow"]:
                    await interaction.response.send_message(
                        "Only the player can start another game against Fishie.",
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
    def __init__(self, controller: "ConnectFourController", ctx: Context):
        super().__init__(timeout=60)
        self.controller = controller
        self.ctx = ctx
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(
            "## Connect Four\nChoose a difficulty to play against Fishie."
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
            self.ctx, difficulty, interaction=interaction
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
    ):
        super().__init__(timeout=60)
        self.controller = controller
        self.ctx = ctx
        self.opponent = opponent
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
            if not game.against_bot or user_id == game.players["yellow"]:
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

    async def start_bot_game(
        self,
        ctx: Context,
        difficulty: str,
        *,
        interaction: discord.Interaction | None = None,
    ) -> ConnectFourGame | None:
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
        names = {
            ctx.author.id: getattr(ctx.author, "name", str(ctx.author.id)),
            bot_id: getattr(self.bot.user, "name", "Fishie"),
        }
        game = ConnectFourGame(
            self,
            ctx,
            {"yellow": ctx.author.id, "red": bot_id},
            names,
            True,
            difficulty,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            ctx.author.id,
        )
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
        game = ConnectFourGame(
            self,
            ctx,
            {"yellow": ctx.author.id, "red": opponent.id},
            names,
            False,
            None,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            ctx.author.id,
        )
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
                (game.players["yellow"],) if game.against_bot else game.player_ids
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
            game.current_color = "yellow"
            game.selected_column = COLS // 2
            game.result = None
            game.forfeited_color = None
            game.move_count = 0
            game.move_history.clear()
            game.recorded = False
            self._register(game)
            view = ConnectFourBoardView(game)
            game.view = view
            if interaction.response.is_done():
                game.message = await interaction.edit_original_response(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await interaction.response.edit_message(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
                game.message = interaction.message
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
            if color != game.current_color:
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
                    game.current_color = "red" if color == "yellow" else "yellow"
                else:
                    game.current_color = "red"
                    ai_column = choose_ai_move(
                        game.board, "red", game.difficulty or "normal"
                    )
                    ai_row = drop_piece(game.board, ai_column, "red")
                    game.record_move(
                        color="red",
                        player_id=game.players["red"],
                        column=ai_column,
                        row=ai_row,
                    )
                    game.selected_column = COLS // 2
                    game.result = board_result(game.board, "red")
                    if game.result is None:
                        game.current_color = "yellow"
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
            await interaction.response.edit_message(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )

    async def record_game(self, game: ConnectFourGame) -> None:
        if game.recorded:
            return
        game.recorded = True
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
