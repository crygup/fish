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
        self._messages: List[Tuple[int, int, int, int, str, datetime.datetime, bool, bool]] = []
        self._messages_attachments: List[Tuple[int, bytes, bool]] = []

        self._deleted_messages: List[Tuple[int, int, int, int, str, datetime.datetime, bool, bool]] = []
        self._deleted_messages_attachments: List[Tuple[int, bytes, bool]] = []

    async def _bulk_insert(self):
        if self._messages:
            sql = """
            INSERT INTO message_logs(author_id, guild_id, channel_id, message_id, message_content, created_at, deleted, has_attachments)
            VALUES($1, $2, $3, $4, $5, $6, $7, $8)
            """
            await self.bot.pool.executemany(sql, self._messages)
            self._messages.clear()

        if self._messages_attachments:
            sql = """
            INSERT INTO message_attachment_logs(message_id, attachment, deleted)
            VALUES($1, $2, $3)
            """
            await self.bot.pool.executemany(sql, self._messages_attachments)
            self._messages_attachments.clear()

        if self._deleted_messages:
            sql = """
            INSERT INTO message_logs(author_id, guild_id, channel_id, message_id, message_content, created_at, deleted, has_attachments)
            VALUES($1, $2, $3, $4, $5, $6, $7, $8)
            """
            await self.bot.pool.executemany(sql, self._deleted_messages)
            self._deleted_messages.clear()

        if self._deleted_messages_attachments:
            sql = """
            INSERT INTO message_attachment_logs(message_id, attachment, deleted)
            VALUES($1, $2, $3)
            """
            await self.bot.pool.executemany(sql, self._deleted_messages_attachments)
            self._deleted_messages_attachments.clear()

    async def cog_unload(self):
        await self._bulk_insert()
        self.bulk_insert.cancel()

    async def cog_load(self) -> None:
        self.bulk_insert.start()

    @tasks.loop(minutes=3.0)
    async def bulk_insert(self):
        await self._bulk_insert()

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.content == "" and message.attachments == []:
            return

        self._messages.append(
            (
                message.author.id,
                message.guild.id,
                message.channel.id,
                message.id,
                message.content or "Message did not contain any content.",
                message.created_at,
                False,
                True if message.attachments != [] else False,
            )
        )
        if message.attachments:
            for attachment in message.attachments:
                self._messages_attachments.append((message.id, await attachment.read(), False))

    async def insert_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.content == "" and message.attachments == []:
            return

        self._deleted_messages.append(
            (
                message.author.id,
                message.guild.id,
                message.channel.id,
                message.id,
                message.content,
                message.created_at,
                True,
                True if message.attachments != [] else False,
            )
        )

        if message.attachments:
            for attachment in message.attachments:
                if not attachment.filename.endswith(("gif", "png", "jpg", "jpeg")):
                    continue
                self._deleted_messages_attachments.append(
                    (message.id, await attachment.read(), True)
                )

    @commands.Cog.listener("on_message_delete")
    async def on_message_delete(self, message: discord.Message):
        await self.insert_message(message)

    @commands.Cog.listener("on_bulk_message_delete")
    async def on_bulk_delete(self, messages: List[discord.Message]):
        for message in messages:
            await self.insert_message(message)
