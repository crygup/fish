from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog
from utils import lastfm_command
if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Lastfm(Cog):
    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.command(name="check", enabled=False)
    @lastfm_command()
    async def command(self, ctx: Context):
        await ctx.send("yes:)")




async def setup(bot: Fishie):
    await bot.add_cog(Lastfm(bot))
