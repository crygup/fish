from __future__ import annotations
import asyncio
from io import BytesIO
import random
import re


import textwrap
from typing import TYPE_CHECKING, Dict, List
import asyncpg

import discord
from discord.ext import commands

from utils import BOT_MENTION_RE, get_pokemon

if TYPE_CHECKING:
    from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(MessageEvents(bot))


class MessageEvents(commands.Cog, name="message_event"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.counter: List[int] = []
        self.message_sql = """
        INSERT INTO messages (  message_id, user_id,
                                channel_id, webhook_id,
                                pinned, edited, deleted,
                                stickers, embeds, attachments,
                                content, created_at,
                                edited_at, deleted_at,
                                guild_id, guild_owner_id)

        VALUES( $1, $2, $3, $4, $5, 
                $6, $7, $8, $9, $10, 
                $11, $12, $13, $14, 
                $15, $16)
        RETURNING *
        """

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

    @commands.Cog.listener("on_message")
    async def pokemon_hint(self, message: discord.Message):
        if (
            message.guild is None
            or message.author.id != 716390085896962058
            or str(message.guild.id)
            not in await self.bot.redis.smembers("poketwo_guilds")
            or str(message.guild.owner_id)
            in await self.bot.redis.smembers("block_list")
            or str(message.guild.id) in await self.bot.redis.smembers("block_list")
            or r"\_" not in message.content
        ):
            return

        to_search = re.match(
            r'the pokémon is (?P<pokemon>[^"]+).', message.content.lower()
        )

        if to_search is None:
            return

        to_search = re.sub(r"\\", "", to_search.groups()[0])
        found = get_pokemon(self.bot, to_search)

        if found == []:
            await message.channel.send("Couldn't find anything matching that, sorry.")
            return

        joined = "\n".join(found)
        await message.channel.send(joined)

    async def add_stickers(
        self,
        message: discord.Message,
        data,
    ):
        sql = """
        INSERT INTO stickers (  description, format, url, 
                                sticker_id, message_id, 
                                created_at)
        VALUES($1, $2, $3, $4, $5, $6)
        """

        for sticker in message.stickers:
            sticker = await sticker.fetch()

            if sticker.format.value == 3:
                continue

            webhook = random.choice(
                [webhook for _, webhook in self.bot.image_webhooks.items()]
            )

            new_message = await webhook.send(
                wait=True,
                files=[
                    discord.File(
                        BytesIO(await sticker.read()),
                        f"{sticker.name}.{sticker.format.name}",
                    )
                ],
            )

            await self.bot.pool.execute(
                sql,
                sticker.description,
                sticker.format.name,
                new_message.attachments[0].url,
                sticker.id,
                data["id"],
                discord.utils.utcnow(),
            )

    async def add_attachments(
        self,
        message: discord.Message,
        data,
    ):
        sql = """
        INSERT INTO attachments (   description, filename, url,
                                    proxy_url, size, height,
                                    width, attachment_id, 
                                    message_id, created_at)
        VALUES( $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10)
        """

        for attachment in message.attachments:
            webhook = random.choice(
                [webhook for _, webhook in self.bot.image_webhooks.items()]
            )

            new_message = await webhook.send(
                wait=True,
                files=[
                    discord.File(
                        BytesIO(await attachment.read()),
                        f"{attachment.filename}",
                    )
                ],
            )

            await self.bot.pool.execute(
                sql,
                attachment.description,
                attachment.filename,
                new_message.attachments[0].url,
                attachment.proxy_url,
                attachment.size,
                attachment.height,
                attachment.width,
                attachment.id,
                data["id"],
                discord.utils.utcnow(),
            )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.guild and message.guild.id == 1014829673311637554:
            return
            # this is the place where we actually
            # are storing the data so we dont want dupes
        data = await self.bot.pool.fetchrow(
            self.message_sql,
            message.id,
            message.author.id,
            message.channel.id,
            message.webhook_id,
            message.pinned,
            False,
            False,
            bool(message.stickers),
            bool(message.embeds),
            bool(message.attachments),
            message.content,
            message.created_at,
            None,
            None,
            message.guild.id if message.guild else None,
            message.guild.owner_id if message.guild else None,
        )

        if message.stickers:
            await self.add_stickers(message, data)

        if message.attachments:
            await self.add_attachments(message, data)

    @commands.Cog.listener("on_message_edit")
    async def on_message_edit(self, before: discord.Message, message: discord.Message):
        if message.guild and message.guild.id == 1014829673311637554:
            return
            # this is the place where we actually
            # are storing the data so we dont want dupes
        data = await self.bot.pool.fetchrow(
            self.message_sql,
            message.id,
            message.author.id,
            message.channel.id,
            message.webhook_id,
            message.pinned,
            True,
            False,
            bool(message.embeds),
            bool(message.stickers),
            bool(message.attachments),
            message.content,
            message.created_at,
            discord.utils.utcnow(),
            None,
            message.guild.id if message.guild else None,
            message.guild.owner_id if message.guild else None,
        )

        if message.stickers:
            await self.add_stickers(message, data)

        if message.attachments:
            await self.add_attachments(message, data)

    @commands.Cog.listener("on_message_delete")
    async def on_message_delete(self, message: discord.Message):
        if message.guild and message.guild.id == 1014829673311637554:
            return
            # this is the place where we actually
            # are storing the data so we dont want dupes
        data = await self.bot.pool.fetchrow(
            self.message_sql,
            message.id,
            message.author.id,
            message.channel.id,
            message.webhook_id,
            message.pinned,
            False,
            True,
            bool(message.stickers),
            bool(message.embeds),
            bool(message.attachments),
            message.content,
            message.created_at,
            None,
            discord.utils.utcnow(),
            message.guild.id if message.guild else None,
            message.guild.owner_id if message.guild else None,
        )

        if message.stickers:
            await self.add_stickers(message, data)

        if message.attachments:
            await self.add_attachments(message, data)
