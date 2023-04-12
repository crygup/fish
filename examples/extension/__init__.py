from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Owner(Cog):
    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    @commands.command(name="accounts")
    async def command(self, ctx: Context):
        await ctx.send("Hello world!")

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        print(f"A message was sent! (ID: {message.id})")


async def setup(bot: Fishie):
    await bot.add_cog(Owner())
