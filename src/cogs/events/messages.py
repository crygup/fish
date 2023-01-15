from __future__ import annotations
import asyncio


import textwrap
from typing import TYPE_CHECKING, List

import discord
from discord.ext import commands

from utils import BOT_MENTION_RE

if TYPE_CHECKING:
    from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(MessageEvents(bot))


class MessageEvents(commands.Cog, name="message_event"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.counter: List[int] = []

    async def insert_message(
        self, message: discord.Message, deleted: bool = False, edited: bool = False
    ):
        guild_id = message.guild.id if message.guild else None

        sql = """
        INSERT INTO message_logs(author_id, guild_id, channel_id, message_id, message_content, created_at, deleted, edited, attachments, embeds, stickers)
        VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """

        await self.bot.pool.execute(
            sql,
            message.author.id,
            guild_id,
            message.channel.id,
            message.id,
            message.content,
            message.created_at,
            deleted,
            edited,
            bool(message.attachments),
            bool(message.embeds),
            bool(message.stickers),
        )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        await self.insert_message(message)

    @commands.Cog.listener("on_message_delete")
    async def on_message_delete(self, message: discord.Message):
        await self.insert_message(message, deleted=True)

    @commands.Cog.listener("on_message_edit")
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        await self.insert_message(after, edited=True)

    @commands.Cog.listener("on_bulk_message_delete")
    async def on_bulk_delete(self, messages: List[discord.Message]):
        for message in messages:
            await self.insert_message(message, deleted=True)

    @commands.Cog.listener("on_message")
    async def on_mention(self, message: discord.Message):
        if message.channel.id in self.counter:
            return

        if message.guild is None:
            return

        if not BOT_MENTION_RE.fullmatch(message.content):
            return

        to_send = f"""
        Hey there, im fishie, a somewhat multipurpose bot but I mainly am focused on logging stuff.

        View your avatars with `fish pfps` or `fish avyh` for a grid of them
        View your names with `fish names`
        View your nicknames with `fish nicknames`

        Wanting auto-reactions to media uploads? Check out `fish auto-reactions toggle`
        Automatic downloads? `fish auto-download set [channel]`
        Automatic Pokétwo hint solving? `fish auto-solve`        

        Feel free to browse {len(self.bot.commands):,} more commands with `fish help`! :3c
        """

        await message.channel.send(textwrap.dedent(to_send))

        # we don't want them spamming it
        self.counter.append(message.channel.id)

        await asyncio.sleep(60)

        try:
            self.counter.remove(message.channel.id)
        except ValueError:
            pass
