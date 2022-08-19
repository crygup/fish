import datetime
from typing import Any, List, Tuple
import discord
from discord.ext import commands, tasks
from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(MessageEvents(bot))


class MessageEvents(commands.Cog, name="message_event"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._deleted_messages_attachments: List[Tuple[int, bytes, bool]] = []

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.content == "" and message.attachments == []:
            return

        sql = """
        INSERT INTO message_logs(author_id, guild_id, channel_id, message_id, message_content, created_at, deleted, has_attachments)
        VALUES($1, $2, $3, $4, $5, $6, $7, $8)
        """
        await self.bot.pool.execute(
            sql,
            message.author.id,
            message.guild.id,
            message.channel.id,
            message.id,
            message.content
            if message.content != ""
            else "Message did not contain any content.",
            message.created_at,
            False,
            True if message.attachments != [] else False,
        )
        if message.attachments:
            messages_attachments = [
                (message.id, await attachment.read(), False)
                for attachment in message.attachments
                if attachment.filename.endswith(("gif", "png", "jpg", "jpeg"))
            ]
            sql = """
            INSERT INTO message_attachment_logs(message_id, attachment, deleted)
            VALUES($1, $2, $3)
            """
            await self.bot.pool.executemany(sql, messages_attachments)

    async def insert_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.content == "" and message.attachments == []:
            return

        sql = """
        INSERT INTO message_logs(author_id, guild_id, channel_id, message_id, message_content, created_at, deleted, has_attachments)
        VALUES($1, $2, $3, $4, $5, $6, $7, $8)
        """

        await self.bot.pool.execute(
            sql,
            message.author.id,
            message.guild.id,
            message.channel.id,
            message.id,
            message.content
            if message.content != ""
            else "Message did not contain any content.",
            message.created_at,
            True,
            True if message.attachments != [] else False,
        )
        if message.attachments:
            messages_attachments = [
                (message.id, await attachment.read(), True)
                for attachment in message.attachments
                if attachment.filename.endswith(("gif", "png", "jpg", "jpeg"))
            ]
            sql = """
            INSERT INTO message_attachment_logs(message_id, attachment, deleted)
            VALUES($1, $2, $3)
            """
            await self.bot.pool.executemany(sql, messages_attachments)

    @commands.Cog.listener("on_message_delete")
    async def on_message_delete(self, message: discord.Message):
        await self.insert_message(message)

    @commands.Cog.listener("on_bulk_message_delete")
    async def on_bulk_delete(self, messages: List[discord.Message]):
        for message in messages:
            await self.insert_message(message)
