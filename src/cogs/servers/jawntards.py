import discord
from discord.ext import commands

from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(JawnServer(bot))


class JawnServer(commands.Cog, name="jawn_server"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def fat_react(self, message: discord.Message):
        if (
            message.author.id != 725443744060538912
            or message.channel.id != 987875558794854451
        ):
            return

        if message.attachments == []:
            return

        emojis = ["\U0001f1eb", "\U0001f1e6", "\U0001f1f9"]
        for emoji in emojis:
            await message.add_reaction(emoji)
