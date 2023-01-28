from __future__ import annotations

from typing import TYPE_CHECKING
from discord.ext import commands

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Example(bot))


class Example(commands.Cog, name="example"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command()
    async def example(self, ctx: Context):
        await ctx.send("This is an example command.")
