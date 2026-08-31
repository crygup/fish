from __future__ import annotations

import asyncio
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import discord

from core.currency import award_daily_capped_coins

from .pvp import DuelBidView

if TYPE_CHECKING:
    from extensions.context import Context


MARKS = ("X", "O")
EMPTY_CELL_LABEL = "\U00002800"
MARK_LABELS = {"X": "\U0000274c", "O": "\U00002b55"}
MOVE_TIMEOUT = 60.0
BOT_WIN_COIN_REWARDS = {
    "easy": (50, 10_000),
    "normal": (200, 10_000),
    "hard": (1_000, 10_000),
}
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
    view: discord.ui.View | discord.ui.LayoutView | None = None
    recorded: bool = False
    coin_reward: int = 0
    # Player-vs-player games reserve one wager per human.  The controller
    # settles these together when the board ends; bot games leave this empty.
    wager_ids: dict[int, int] = field(default_factory=dict)
    wager_stakes: dict[int, int] = field(default_factory=dict)
    wager_stake: int = 0
    # The winner's total PvP wager pool is retained for the completed-board
    # summary after each reservation has been settled and cleared.
    pvp_payout: int = 0
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
        self.coin_reward = 0
        self.wager_ids.clear()
        self.wager_stakes.clear()
        self.wager_stake = 0
        self.pvp_payout = 0
        self.started_at = discord.utils.utcnow()
        self.view = None
        self.move_timeout_task = None


class TicTacToeView(discord.ui.LayoutView):
    """Components V2 Tic-Tac-Toe board.

    The board and its controls live in one container so Discord renders the
    game as a single CV2 card.  The rematch action intentionally remains in a
    separate row below that card, matching the other minigame views.
    """

    def __init__(self, game: TicTacToeGame):
        super().__init__(timeout=900)
        self.game = game
        self.cells: list[discord.ui.Button] = []
        rows: list[discord.ui.ActionRow] = []
        for index in range(9):
            button = discord.ui.Button(
                label=EMPTY_CELL_LABEL,
                style=discord.ButtonStyle.secondary,
            )

            async def callback(
                interaction: discord.Interaction, cell_index: int = index
            ) -> None:
                await self._select_cell(interaction, cell_index)

            button.callback = callback
            self.cells.append(button)
            if index % 3 == 0:
                rows.append(discord.ui.ActionRow())
            rows[-1].add_item(button)

        self.rematch = discord.ui.Button(
            label="Play again",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.rematch.callback = self._play_again
        self.display = discord.ui.TextDisplay(self.content())
        self.footer = discord.ui.TextDisplay(self.footer_content())
        self.container = discord.ui.Container(
            self.display,
            *rows,
            discord.ui.Separator(),
            self.footer,
            accent_color=game.controller.bot.embedcolor,
        )
        self.add_item(self.container)
        self.add_item(discord.ui.ActionRow(self.rematch))
        self.refresh()

    def content(self) -> str:
        title = "## Tic-Tac-Toe"
        if self.game.against_bot and self.game.difficulty:
            title += f" · {self.game.difficulty.title()}"
        return title

    def footer_content(self) -> str:
        player_x = discord.utils.escape_markdown(
            self.game.names[self.game.players["X"]]
        )
        player_o = discord.utils.escape_markdown(
            self.game.names[self.game.players["O"]]
        )
        if self.game.result == "draw":
            status = "Draw game."
        elif self.game.result in MARKS:
            winner = discord.utils.escape_markdown(
                self.game.names[self.game.players[self.game.result]]
            )
            if self.game.forfeited_mark is not None:
                forfeited = discord.utils.escape_markdown(
                    self.game.names[self.game.players[self.game.forfeited_mark]]
                )
                status = f"{forfeited} forfeited after 60 seconds. {winner} wins!"
            else:
                status = f"{winner} wins!"
            if self.game.pvp_payout:
                status = f"{winner} wins {self.game.pvp_payout:,} Coins!"
            if self.game.coin_reward:
                status += f" · Earned {self.game.coin_reward:,} Coins"
        else:
            current = discord.utils.escape_markdown(
                self.game.names[self.game.players[self.game.current_mark]]
            )
            status = f"{current}'s turn"
        return (
            f"{MARK_LABELS['X']} {player_x} · "
            f"{MARK_LABELS['O']} {player_o} · {status}"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.view is self and interaction.user.id in self.game.player_ids:
            return True
        await interaction.response.send_message(
            "This Tic-Tac-Toe game is no longer active.", ephemeral=True
        )
        return False

    def refresh(self) -> None:
        self.display.content = self.content()
        self.footer.content = self.footer_content()
        for index, button in enumerate(self.cells):
            mark = self.game.board[index]
            button.label = EMPTY_CELL_LABEL if mark is None else MARK_LABELS[mark]
            button.disabled = self.game.result is not None
            # The mark emoji communicates X/O; keep every button the default
            # grey so the board matches the other CV2 minigame controls.
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
            self.refresh()
            if interaction.message is not None:
                self.game.message = await interaction.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                self.game.message = await interaction.edit_original_response(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
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
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
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
            for button in self.cells:
                button.disabled = True
            self.rematch.disabled = True
            if self.game.message is not None:
                try:
                    await self.game.message.edit(
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass


class TicTacToeDifficultyView(discord.ui.LayoutView):
    def __init__(self, controller: TicTacToeController, game: TicTacToeGame):
        super().__init__(timeout=60)
        self.controller = controller
        self.game = game
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(
            "## Tic-Tac-Toe\nChoose a difficulty to play Fishie again."
        )
        self.buttons: list[discord.ui.Button] = []

        for difficulty in ("easy", "normal", "hard"):
            button = discord.ui.Button(
                label=difficulty.title(), style=discord.ButtonStyle.secondary
            )

            async def callback(
                interaction: discord.Interaction, selected: str = difficulty
            ) -> None:
                await self._select_difficulty(interaction, selected)

            button.callback = callback
            self.buttons.append(button)

        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(*self.buttons),
            accent_color=controller.bot.embedcolor,
        )
        self.add_item(self.container)

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
            for item in self.buttons:
                item.disabled = True
            self.display.content = "## Tic-Tac-Toe\nThis game setup expired."
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass


class TicTacToeRematchView(discord.ui.LayoutView):
    def __init__(self, controller: TicTacToeController, game: TicTacToeGame):
        super().__init__(timeout=60)
        self.controller = controller
        self.game = game
        self.confirmed: set[int] = set()
        self.message: discord.Message | None = None
        self.display = discord.ui.TextDisplay(
            "## Tic-Tac-Toe rematch\n"
            "Both players must confirm before the rematch starts."
        )
        self.confirm_button = discord.ui.Button(
            label="Confirm rematch", style=discord.ButtonStyle.secondary
        )
        self.decline_button = discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.secondary
        )
        self.confirm_button.callback = self.confirm
        self.decline_button.callback = self.decline
        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(self.confirm_button, self.decline_button),
            accent_color=controller.bot.embedcolor,
        )
        self.add_item(self.container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game.view is self and interaction.user.id in self.game.player_ids:
            return True
        await interaction.response.send_message(
            "This rematch is no longer active.",
            ephemeral=True,
        )
        return False

    async def confirm(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                await interaction.response.send_message(
                    "This rematch is no longer active.", ephemeral=True
                )
                return
            self.confirmed.add(interaction.user.id)
            confirmed = self.confirmed == set(self.game.player_ids)
            if not confirmed:
                self.display.content = (
                    "## Tic-Tac-Toe rematch\n"
                    f"{self.controller.player_name(interaction.user.id)} confirmed. "
                    "Waiting for the other player."
                )
                await interaction.response.edit_message(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        if confirmed:
            await self.controller.restart_game(self.game, None, interaction)
            self.stop()

    async def decline(self, interaction: discord.Interaction) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                await interaction.response.send_message(
                    "This rematch is no longer active.", ephemeral=True
                )
                return
            self.controller.remove_game(self.game)
            self.game.view = None
            self.confirm_button.disabled = True
            self.decline_button.disabled = True
            self.display.content = "## Tic-Tac-Toe rematch\nRematch declined."
            self.stop()
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_timeout(self) -> None:
        async with self.game.lock:
            if self.game.view is not self:
                return
            self.controller.remove_game(self.game)
            self.game.view = None
            self.confirm_button.disabled = True
            self.decline_button.disabled = True
            self.display.content = "## Tic-Tac-Toe rematch\nRematch expired."
            if self.message is not None:
                try:
                    await self.message.edit(
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass


class TicTacToeChallengeView(discord.ui.LayoutView):
    """Ask the challenged user to accept a player-versus-player game."""

    def __init__(
        self,
        controller: TicTacToeController,
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
            f"## Tic-Tac-Toe\n{opponent.mention}, you were challenged to a "
            f"Tic-Tac-Toe duel against **{discord.utils.escape_markdown(ctx.author.display_name)}**. "
            "Do you want to play?"
        )
        self.accept_button = discord.ui.Button(
            label="Accept", style=discord.ButtonStyle.secondary
        )
        self.decline_button = discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.secondary
        )
        self.accept_button.callback = self.accept
        self.decline_button.callback = self.decline
        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(self.accept_button, self.decline_button),
            accent_color=controller.bot.embedcolor,
        )
        self.add_item(self.container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opponent.id:
            return True
        await interaction.response.send_message(
            "Only the challenged user can answer this invitation.", ephemeral=True
        )
        return False

    def disable_all(self) -> None:
        self.accept_button.disabled = True
        self.decline_button.disabled = True

    async def accept(self, interaction: discord.Interaction) -> None:
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
                        "Failed to start wagered Tic-Tac-Toe duel"
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
                game_name="Tic-Tac-Toe",
                challenger_bid=self.challenger_bid,
                on_ready=on_ready,
            )
            bid_view.message = self.message
            await interaction.response.edit_message(
                # The challenge and bid prompt are both Components V2 views.
                view=bid_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.stop()
            return
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

    async def decline(self, interaction: discord.Interaction) -> None:
        self.disable_all()
        self.stop()
        self.display.content = "## Tic-Tac-Toe\nChallenge declined."
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        self.disable_all()
        self.display.content = "## Tic-Tac-Toe\nChallenge expired."
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass


class TicTacToeModeView(discord.ui.LayoutView):
    def __init__(
        self,
        controller: TicTacToeController,
        ctx: Context,
        prompt: str | None = None,
    ):
        super().__init__(timeout=60)
        self.controller = controller
        self.ctx = ctx
        self.message: discord.Message | None = None
        self.selecting = False
        self.display = discord.ui.TextDisplay(
            "## Tic-Tac-Toe\n"
            + (prompt or "Choose a difficulty below to play against Fishie.")
        )
        self.buttons: list[discord.ui.Button] = []

        for difficulty in ("easy", "normal", "hard"):
            button = discord.ui.Button(
                label=difficulty.title(), style=discord.ButtonStyle.secondary
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
            self.buttons.append(button)

        self.container = discord.ui.Container(
            self.display,
            discord.ui.ActionRow(*self.buttons),
            accent_color=controller.bot.embedcolor,
        )
        self.add_item(self.container)

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
        for item in self.buttons:
            item.disabled = True
        self.display.content = "## Tic-Tac-Toe\nThis game setup expired."
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
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

    async def award_bot_win(self, game: TicTacToeGame) -> int:
        """Award a capped daily Coin reward for a human win against Fishie."""

        if not game.against_bot or game.result not in MARKS:
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
            f"game_tictactoe_{difficulty}",
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
                view = game.view

            if game.message is not None and isinstance(view, TicTacToeView):
                view.refresh()
                await game.message.edit(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
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

        if interaction is None:
            try:
                game.message = await ctx.send(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                self.remove_game(game)
                raise
        else:
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    game.message = interaction.message
                elif interaction.message is not None:
                    game.message = await interaction.message.edit(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    game.message = await interaction.edit_original_response(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
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
        stakes: tuple[int, int] | None = None,
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
            # all new settlement paths use the per-player mapping.
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
            # Escrow both players before exposing the board.  If the second
            # wallet cannot cover its stake, return the first reservation and
            # leave no partial duel behind.
            try:
                for player_id, player_stake in wager_stakes.items():
                    wager = await self.bot.currency.open_wager(
                        player_id,
                        player_stake,
                        source="pvp_tictactoe",
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
                            "Failed to refund a Tic-Tac-Toe duel wager"
                        )
                raise

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
            wager_ids=wager_ids,
            wager_stakes=wager_stakes,
            wager_stake=wager_stake,
        )
        self._register_game(game)
        view = TicTacToeView(game)
        game.view = view
        try:
            if interaction is None:
                game.message = await ctx.send(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    game.message = interaction.message
                elif interaction.message is not None:
                    game.message = await interaction.message.edit(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    game.message = await interaction.edit_original_response(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
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
                        "Failed to refund Tic-Tac-Toe wagers after startup failed"
                    )
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
            if game.view is None:
                message = "This Tic-Tac-Toe game setup has expired."
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
                return

            active = self._active_game(game.player_ids, against_bot=game.against_bot)
            if active is not None and active is not game:
                message = "You already have another active Tic-Tac-Toe game."
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
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

            if interaction.response.is_done():
                try:
                    game.message = await interaction.edit_original_response(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    self.remove_game(game)
                    raise
            else:
                try:
                    await interaction.response.edit_message(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    game.message = interaction.message
                except Exception:
                    self.remove_game(game)
                    raise
            self.schedule_move_timeout(game)

    async def record_game(self, game: TicTacToeGame) -> None:
        if game.recorded:
            return
        game.recorded = True
        try:
            game.coin_reward = await self.award_bot_win(game)
        except Exception:
            self.bot.logger.exception("Failed to award Tic-Tac-Toe Coins")
        if game.wager_ids:
            # The losing stake is already debited.  A winner receives the
            # complete two-player pool; a draw refunds both reservations.
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
                elif game.result in MARKS:
                    winner_id = game.players[game.result]
                    game.pvp_payout = pool
                    for player_id, wager_id in game.wager_ids.items():
                        payout = pool if player_id == winner_id else 0
                        await self.bot.currency.settle_wager(wager_id, payout)
            except Exception:
                self.bot.logger.exception("Failed to settle Tic-Tac-Toe player wagers")
            finally:
                game.wager_ids.clear()
                game.wager_stakes.clear()
                game.wager_stake = 0
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
        description = (
            f"{MARK_LABELS['X']} {player_x}\n"
            f"{MARK_LABELS['O']} {player_o}\n\n{status}"
        )
        if game.coin_reward and game.result in MARKS:
            winner = discord.utils.escape_markdown(
                game.names[game.players[game.result]]
            )
            # Keep the compatibility embed's result summary in the same
            # single-line footer style as the Components V2 board.  The board
            # itself uses a native ``discord.ui.Separator``; never render a
            # hand-written line of dashes here.
            description += (
                "\n\n"
                f"-# {winner} won {game.coin_reward:,} coins · "
                f"{winner} earned **{game.coin_reward:,} Coins**."
            )
        return discord.Embed(
            title=title,
            description=description,
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
