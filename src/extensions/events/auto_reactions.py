from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    pass


class Reactions(Cog):
    async def add_reactions(self, message: discord.Message):
        if message.guild is None:
            return

        if message.guild.id not in self.bot.db_cache.auto_reaction_guilds:
            return
        if not self.bot.db_cache.auto_reaction_channel_allowed(
            message.guild.id, message.channel.id
        ):
            return

        if message.attachments:
            await self.bot.add_reactions(
                message, ["\U00002b06\U0000fe0f", "\U00002b07\U0000fe0f"]
            )

        for embed in message.embeds:
            if embed.type != "rich":
                await self.bot.add_reactions(
                    message, ["\U00002b06\U0000fe0f", "\U00002b07\U0000fe0f"]
                )

    @commands.Cog.listener("on_message")
    async def reaction_message(self, message: discord.Message):
        await self.add_reactions(message)

    @commands.Cog.listener("on_message_edit")
    async def reaction_edit(self, _, message: discord.Message):
        await self.add_reactions(message)
