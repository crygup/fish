import discord
from discord.ext import commands
from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(Example(bot))


class Example(commands.Cog, name="example"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command()
    async def example(self, ctx: commands.Context):
        await ctx.send("This is an example command.")
