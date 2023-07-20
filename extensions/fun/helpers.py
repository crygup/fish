import random
from typing import Any, Coroutine, Literal, Optional, Union, TypeAlias

import discord
from dataclasses import dataclass
from enum import Enum

from discord.interactions import Interaction
from discord.ui.item import Item
from extensions.context import Context
from utils import AuthorView
from discord.ext import commands

Choice: TypeAlias = Union[Literal["rock"], Literal["paper"], Literal["scissors"]]

class RPSWin(Enum):
    win = 1
    loss = 2
    draw = 3

@dataclass()
class RPSResults:
    user_choice: Choice
    bot_choice: Choice
    result: RPSWin

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
    
    async def send_result(
        self, choice: Choice, interaction: discord.Interaction, button: discord.ui.Button
    ):  
        choices = {
            "rock": self.rock_choice,
            "paper": self.paper_choice,
            "scissors": self.scissors_choice
        }
        answer = {
            "win": "You win!",
            "loss": "You lost!",
            "draw": "We draw!"
        }
        style = {
            "win": discord.ButtonStyle.green,
            "loss": discord.ButtonStyle.red,
            "draw": discord.ButtonStyle.blurple,
        }

        results = choices[choice]()

        button.style = style[results.result.name]

        self.disable_all()

        if interaction.message is None:
            raise commands.BadArgument("Somehow the message was empty.")

        await interaction.message.edit(
            content=f"{answer[results.result.name]} I chose {self.bot_choice}.", view=self
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