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
        if message.guild is None:
            return
        if message.author.bot:
            return
        if str(message.guild.id) not in await self.bot.redis.smembers("auto_reactions"):
            return

        if message.attachments:
            return await self.add_reactions(message)

        embeds = message.embeds

        for embed in embeds:
            if embed.type != "rich":
                return await self.add_reactions(message)

    @commands.Cog.listener("on_message_edit")
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None:
            return
        if before.author.bot:
            return
        if str(before.guild.id) not in await self.bot.redis.smembers("auto_reactions"):
            return

        if after.attachments:
            return await self.add_reactions(after)

        embeds = after.embeds

        for embed in embeds:
            if embed.type != "rich":
                return await self.add_reactions(after)
