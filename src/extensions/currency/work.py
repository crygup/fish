"""Interactive tasks used by the ``work`` Coins command.

The views in this module deliberately keep their state in memory. Tasks are
short-lived, and the currency cog owns the idempotent reward operation. This
keeps a button retry or a modal submitted twice from ever paying a task more
than once.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import discord

from extensions.fun.wordle import (
    WORDLE_WORDS,
    WordleBoardView,
    WordleGame,
    message_guess,
)
from utils.paths import FILES_ROOT

WORK_WORDLE_PATH = FILES_ROOT / "data" / "wordle.txt"
_FALLBACK_WORDLE_WORDS = (
    "apple",
    "beach",
    "bread",
    "chair",
    "cloud",
    "grape",
    "house",
    "lemon",
    "mouse",
    "plant",
    "river",
    "stone",
)


def _load_wordle_words() -> tuple[str, ...]:
    try:
        lines = WORK_WORDLE_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    words = tuple(
        dict.fromkeys(
            word.strip().casefold()
            for word in lines
            if len(word.strip()) == 5 and word.strip().isalpha()
        )
    )
    return words or _FALLBACK_WORDLE_WORDS


WORK_WORDLE_WORDS = _load_wordle_words()


WorkFinish = Callable[[bool], Awaitable[None]]
WorkClick = Callable[[], Awaitable[int | None]]


class WorkMathModal(discord.ui.Modal, title="Solve the math task"):
    """Modal used by :class:`WorkMathView` for button-based answers."""

    answer = discord.ui.TextInput(
        label="Answer",
        placeholder="Enter the answer",
        required=True,
        max_length=32,
    )

    def __init__(self, view: "WorkMathView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.view.submit_interaction(interaction, str(self.answer.value))


class WorkMathView(discord.ui.View):
    """Persistent answer prompt for a ``work`` math task.

    The question remains in the original message for the complete lifetime
    of the task.  Incorrect modal guesses are acknowledged only by an
    ephemeral ``Incorrect!`` response.  Typed guesses remain silent, so
    neither kind of wrong answer replaces the question or removes the button.
    """

    def __init__(
        self,
        user_id: int,
        question: str,
        answer: int,
        on_complete: WorkFinish,
        *,
        source_message_id: int | None = None,
    ) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.question = question
        self.answer = answer
        self.on_complete = on_complete
        # The command message can race the cog's on_message listener.  Keep
        # its id so ``fish work`` is never interpreted as an answer.
        self.source_message_id = source_message_id
        self.finished = False
        self.message: discord.Message | None = None
        self.lock = asyncio.Lock()
        self.answer_button = discord.ui.Button(
            label="Enter answer", style=discord.ButtonStyle.secondary
        )
        self.answer_button.callback = self._open_modal
        self.add_item(self.answer_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This work task belongs to another user.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        if self.finished:
            await interaction.response.send_message(
                "This work task is already complete.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(WorkMathModal(self))

    def _is_correct(self, raw_answer: str) -> bool:
        try:
            return int(raw_answer.strip()) == self.answer
        except (TypeError, ValueError):
            return False

    async def _edit_prompt(self, content: str | None = None) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=self.question if content is None else content,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    def _success_text(self) -> str:
        return f"Solved! {self.question}"

    def _timeout_text(self) -> str:
        return f"Times up! {self.question}\nThe answer was **{self.answer}**."

    async def _complete(self) -> bool:
        """Mark the task complete exactly once and settle its reward."""

        if self.finished:
            return False
        self.finished = True
        self.answer_button.disabled = True
        self.stop()
        await self.on_complete(True)
        return True

    async def submit_interaction(
        self, interaction: discord.Interaction, raw_answer: str
    ) -> None:
        """Handle an answer submitted through the modal."""

        async with self.lock:
            if self.finished:
                await interaction.response.send_message(
                    "This work task is already complete.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not self._is_correct(raw_answer):
                await interaction.response.send_message(
                    "Incorrect!",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            await interaction.response.send_message(
                "Correct!",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._complete()
            await self._edit_prompt(self._success_text())

    async def submit_message(self, message: discord.Message) -> None:
        """Handle an answer typed as a normal Discord message."""

        async with self.lock:
            if (
                self.finished
                or message.author.id != self.user_id
                or (
                    self.source_message_id is not None
                    and getattr(message, "id", None) == self.source_message_id
                )
            ):
                return
            if not self._is_correct(message.content):
                # Chat guesses are silent; only modal submissions receive an
                # ephemeral acknowledgement for an incorrect answer.
                return

            await self._complete()
            await self._edit_prompt(self._success_text())

    async def on_timeout(self) -> None:
        async with self.lock:
            if self.finished:
                return
            self.finished = True
            self.answer_button.disabled = True
            self.stop()
            await self._edit_prompt(self._timeout_text())
        await self.on_complete(False)


class WorkClickView(discord.ui.LayoutView):
    """The normal click display with a private ten-click work goal.

    The global counter is rendered using the same Components V2 layout as the
    standalone ``click`` command.  Each button press still goes through the
    Fun cog's normal counter writer, so global, per-user, and per-guild click
    totals have exactly the same semantics as regular clicks.
    """

    def __init__(
        self,
        user_id: int,
        on_complete: WorkFinish,
        on_click: WorkClick | None = None,
        total: int = 0,
    ) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.on_complete = on_complete
        self.on_click = on_click
        self.total = total
        self.clicks = 0
        self.finished = False
        self.message: discord.Message | None = None
        self.lock = asyncio.Lock()
        self.counter = discord.ui.TextDisplay(self._counter_text())
        self.status = discord.ui.Button(
            label="Click 0/10", style=discord.ButtonStyle.primary
        )
        self.status.callback = self._click
        self.add_item(
            discord.ui.Container(
                self.counter,
                discord.ui.Separator(),
                discord.ui.ActionRow(self.status),
            )
        )

    def _counter_text(self) -> str:
        return f"## Click\n**Global clicks:** {self.total:,}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "This work task belongs to another user.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _click(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.finished:
                await interaction.response.send_message(
                    "This work task is already complete.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if self.on_click is not None:
                try:
                    total = await self.on_click()
                    if isinstance(total, int):
                        self.total = total
                except Exception:
                    # A metrics write must never make a valid work click fail.
                    pass
            self.clicks += 1
            self.status.label = f"Click {self.clicks}/10"
            self.counter.content = self._counter_text()
            completed = self.clicks >= 10
            if completed:
                self.finished = True
                self.status.disabled = True
                self.stop()
            await interaction.response.edit_message(view=self)
        if completed:
            await self.on_complete(True)

    async def on_timeout(self) -> None:
        async with self.lock:
            if self.finished:
                return
            self.finished = True
            self.status.disabled = True
            self.stop()
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass
        await self.on_complete(False)


class WorkWordleView(WordleBoardView):
    """The normal Wordle board wired to a Coins task completion callback.

    Reusing :class:`WordleBoardView` keeps the work task's board, modal,
    colourblind palette, hard-mode validation, timeout, and Components V2
    layout identical to the standalone Wordle command.
    """

    def __init__(self, game: WordleGame, on_complete: WorkFinish) -> None:
        self.on_complete = on_complete
        super().__init__(
            game,
            self._submit_guess,
            on_timeout=self._timed_out,
            on_give_up=self._give_up,
        )

    async def _give_up(
        self, interaction: discord.Interaction, game: WordleGame
    ) -> None:
        """Give up the work Wordle task and award its minimum payout."""

        async with game.lock:
            if game.completed:
                await interaction.response.send_message(
                    "This Wordle game is already over.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            game.result = "lost"
            self.refresh()
            self.stop()
            try:
                await interaction.response.edit_message(
                    view=self,
                    attachments=[self.board_file],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                # The task still needs to settle if the interaction expired
                # while the user was pressing the button.
                pass
        await self.on_complete(False)

    async def _submit_guess(
        self, interaction: discord.Interaction, game: WordleGame, raw_guess: str
    ) -> None:
        async with game.lock:
            try:
                game.submit(raw_guess, set(WORDLE_WORDS))
            except ValueError as error:
                await interaction.response.send_message(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            self.refresh()
            finished = game.completed
            if finished:
                self.stop()
            await interaction.response.edit_message(
                view=self,
                attachments=[self.board_file],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if finished:
                await self.on_complete(game.result == "won")

    async def submit_message(self, message: discord.Message) -> None:
        """Submit a five-letter chat message using the modal's game path."""

        guess = message_guess(message, self.game)
        if guess is None:
            return
        async with self.game.lock:
            try:
                self.game.submit(guess, set(WORDLE_WORDS))
            except ValueError as error:
                try:
                    await message.reply(
                        str(error),
                        delete_after=5,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
                return

            self.refresh()
            finished = self.game.completed
            if finished:
                self.stop()
            if self.game.message is not None:
                try:
                    await self.game.message.edit(
                        view=self,
                        attachments=[self.board_file],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
            if finished:
                await self.on_complete(self.game.result == "won")

    async def _timed_out(self, _game: WordleGame) -> None:
        self.refresh()
        if self.game.message is not None:
            try:
                await self.game.message.edit(
                    view=self,
                    attachments=[self.board_file],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
        await self.on_complete(False)


__all__ = [
    "WorkClickView",
    "WorkMathView",
    "WorkWordleView",
    "WORK_WORDLE_WORDS",
]
