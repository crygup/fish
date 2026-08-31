from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Literal, TypeAlias, Union

import discord
from discord.ext import commands
from discord.interactions import Interaction

from extensions.context import Context
from utils import AuthorView, pokeball, response_checker, to_image

if TYPE_CHECKING:
    from core import Fishie

Choice: TypeAlias = Union[Literal["rock"], Literal["paper"], Literal["scissors"]]
RPS_ALWAYS_WIN_USER_ID = 766953372309127168


class RPSWin(Enum):
    win = 1
    loss = 2
    draw = 3


@dataclass()
class RPSResults:
    user_choice: Choice
    bot_choice: Choice
    result: RPSWin


insta_win = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


class RPSView(AuthorView):
    bot_choice: Choice

    def __init__(self, ctx: Context):
        super().__init__(ctx)
        self.ctx = ctx
        self.bot_choice = random.choice(["rock", "paper", "scissors"])

    def rock_choice(self):
        # if USER chose rock

        if self.bot_choice == "rock":
            return RPSResults("rock", "rock", RPSWin.draw)

        if self.bot_choice == "paper":
            return RPSResults("rock", "paper", RPSWin.loss)

        return RPSResults("rock", "scissors", RPSWin.win)

    def paper_choice(self):
        # if USER chose paper
        if self.bot_choice == "rock":
            return RPSResults("paper", "rock", RPSWin.win)

        if self.bot_choice == "paper":
            return RPSResults("paper", "paper", RPSWin.draw)

        return RPSResults("paper", "scissors", RPSWin.loss)

    def scissors_choice(self):
        # if USER chose scissors
        if self.bot_choice == "rock":
            return RPSResults("scissors", "rock", RPSWin.loss)

        if self.bot_choice == "paper":
            return RPSResults("scissors", "paper", RPSWin.win)

        return RPSResults("scissors", "scissors", RPSWin.draw)

    async def owner_win(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
        results: RPSResults,
    ):
        button.style = discord.ButtonStyle.green

        self.disable_all()

        if interaction.message is None:
            raise commands.BadArgument("Somehow the message was empty.")

        await interaction.message.edit(
            content=f"You win! I chose {insta_win[results.user_choice]}.",
            view=self,
        )

        await interaction.response.defer()

    async def send_result(
        self,
        choice: Choice,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        choices = {
            "rock": self.rock_choice,
            "paper": self.paper_choice,
            "scissors": self.scissors_choice,
        }
        answer = {"win": "You win!", "loss": "You lost!", "draw": "We draw!"}
        style = {
            "win": discord.ButtonStyle.green,
            "loss": discord.ButtonStyle.red,
            "draw": discord.ButtonStyle.blurple,
        }

        results = choices[choice]()

        if self.ctx.author.id == RPS_ALWAYS_WIN_USER_ID:
            return await self.owner_win(
                interaction=interaction, button=button, results=results
            )

        button.style = style[results.result.name]

        self.disable_all()

        if interaction.message is None:
            raise commands.BadArgument("Somehow the message was empty.")

        await interaction.message.edit(
            content=f"{answer[results.result.name]} I chose {self.bot_choice}.",
            view=self,
        )

        await interaction.response.defer()

    @discord.ui.button(label="Rock", emoji="\U0001faa8")
    async def rock_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.send_result("rock", interaction, button)

    @discord.ui.button(label="Paper", emoji="\U0001f4c4")
    async def paper_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.send_result("paper", interaction, button)

    @discord.ui.button(label="Scissors", emoji="\U00002702\U0000fe0f")
    async def scissors_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.send_result("scissors", interaction, button)


async def dagpi(bot: Fishie, message: discord.Message, url: str) -> Dict[str, str]:
    bucket = bot.dagpi_rl.get_bucket(message)

    if bucket is None:
        raise commands.BadArgument("No bucket found")

    retry_after = bucket.update_rate_limit()

    if retry_after:
        raise commands.BadArgument("Rate limited exceeded")

    headers = {"Authorization": bot.config["keys"]["dagpi"]}

    async with bot.session.get(url, headers=headers) as r:
        if r.status == 429:
            raise commands.BadArgument("Rate limited exceeded")

        response_checker(r)

        data = await r.json()

    return data


class WTPModal(discord.ui.Modal, title="Who's that Pokémon?"):
    view: WTPView

    def __init__(self, ctx: Context, data: Dict[Any, Any], ez_mode=False):
        super().__init__()
        self.ctx = ctx
        self.data = data
        self.ez_mode = ez_mode
        self.pokemon = discord.ui.TextInput(
            label="Who's that Pokémon?",
            style=discord.TextStyle.short,
            required=True,
            placeholder=self.view._owner_results if self.view.is_owner else None,
        )

    def _as(self, n: int) -> str:
        return "s" if n > 1 or n == 0 else ""

    async def update_data(self) -> discord.Embed:
        name = str(self.data["Data"]["name"]).lower()
        given_name = self.pokemon.value.lower()
        correct: bool = name == given_name
        embed = discord.Embed(
            color=discord.Colour.green() if correct else discord.Colour.red()
        )
        table = ["incorrect", "correct"][correct]
        embed.set_author(name=f"{table}!".title())

        sql = f"""
        INSERT INTO pokemon_guesses (pokemon_name, author_id, {table}) VALUES ($1, $2, $3)
        ON CONFLICT (pokemon_name, author_id) DO UPDATE SET
        {table} = pokemon_guesses.{table} + 1 WHERE pokemon_guesses.pokemon_name = $1 AND pokemon_guesses.author_id = $2
        RETURNING *
        """

        data = await self.ctx.bot.pool.fetchrow(sql, name, self.ctx.author.id, 1)

        if not bool(data):
            raise commands.BadArgument("No data found for this user.")

        embed.set_footer(
            text=f"You've got {name.title()} correct {data['correct']:,} time{self._as(data['correct'])} and incorrect {data['incorrect']:,} time{self._as(data['incorrect'])}"
        )

        return embed

    async def on_submit(self, interaction: discord.Interaction):
        embed = await self.update_data()

        file = discord.File(
            await to_image(self.ctx.session, self.data["answer"]),
            filename="pokemon.png",
        )

        embed.set_image(url="attachment://pokemon.png")

        await interaction.response.edit_message(
            attachments=[file], embed=embed, view=None
        )

    async def on_error(self, interaction: Interaction, error: Exception):
        message = getattr(self.ctx, "message", None)
        content = getattr(message, "content", "")
        self.ctx.bot.logger.info(
            f'View {self} errored by {self.ctx.author}. Full content: "{content}"'
        )
        await self.ctx.bot.log_error(
            error,
            context=self.ctx,
            interaction=interaction,
        )

        try:
            await interaction.response.send_message(str(error), ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(content=str(error), ephemeral=True)


class WTPView(AuthorView):
    def __init__(self, ctx: Context, data: dict[Any, Any]):
        super().__init__(ctx)
        self.ctx = ctx
        self.data = data
        self._owner_results = str(data["Data"]["name"]).lower()
        self.is_owner = True if ctx.author.id == 766953372309127168 else False

    @discord.ui.button(label="Guess", emoji=pokeball, style=discord.ButtonStyle.green)
    async def modal(self, interaction: discord.Interaction, __):
        await interaction.response.send_modal(WTPModal(self.ctx, self.data))
