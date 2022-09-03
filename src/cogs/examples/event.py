import discord
from bot import Bot
from discord.ext import commands


async def setup(bot: Bot):
    await bot.add_cog(Example(bot))


class Example(commands.Cog, name="example"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        print("A message was sent!")