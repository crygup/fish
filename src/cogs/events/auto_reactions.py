from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(AutoReactions(bot))


class AutoReactions(commands.Cog, name="auto_reactions"):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def add_reactions(self, message: discord.Message):
        reactions = ["\U00002b06\U0000fe0f", "\U00002b07\U0000fe0f"]
        for reaction in reactions:
            try:
                await message.add_reaction(reaction)
            except:  # bare except cuz i dont really need anything to happen nor will this failing raise any suspicion
                pass

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if (
            message.guild is None
            or message.author.bot
            or str(message.guild.id)
            not in await self.bot.redis.smembers("auto_reactions")
        ):
            return

        if message.attachments:
            await self.add_reactions(message)
            return

        for embed in message.embeds:
            if not embed.type == "rich":
                await self.add_reactions(message)
                return
