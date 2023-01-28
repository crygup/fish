from __future__ import annotations
import asyncio
import re


import textwrap
from typing import TYPE_CHECKING, List

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
