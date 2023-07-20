from __future__ import annotations
import random

from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from core import Cog
from .helpers import RPSView

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Fun(Cog):
    """Fun miscellaneous commands"""
    emoji = discord.PartialEmoji(name="\U0001f604")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.command(name="rock-paper-scissors", aliases=("rockpaperscissors", "rps"))
    async def RPSCommand(self, ctx: Context):
        await ctx.send(view=RPSView(ctx))

    @commands.command(name='monark')
    async def monark(self, ctx: Context):
        """monark said this"""
        files = [r'.\files\monark\monark.jpg', r'.\files\monark\monark2.png']
        await ctx.send(file=discord.File(random.choice(files), "monark.png"))

async def setup(bot: Fishie):
    await bot.add_cog(Fun(bot))
