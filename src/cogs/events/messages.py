from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(MessageEvents(bot))


class MessageEvents(commands.Cog, name="message_event"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._deleted_messages_attachments: List[Tuple[int, bytes, bool]] = []

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        await self.insert_message(message)

    async def insert_message(self, message: discord.Message, deleted: bool = False):
        if message.guild is None:
            return

        if message.content == "":
            return

        sql = """
        INSERT INTO message_logs(author_id, guild_id, channel_id, message_id, message_content, created_at, deleted)
        VALUES($1, $2, $3, $4, $5, $6, $7)
        """
        await self.bot.pool.execute(
            sql,
            message.author.id,
            message.guild.id,
            message.channel.id,
            message.id,
            message.content,
            message.created_at,
            deleted,
        )

    @commands.Cog.listener("on_message_delete")
    async def on_message_delete(self, message: discord.Message):
        await self.insert_message(message, True)

    @commands.Cog.listener("on_bulk_message_delete")
    async def on_bulk_delete(self, messages: List[discord.Message]):
        for message in messages:
            await self.insert_message(message, True)
