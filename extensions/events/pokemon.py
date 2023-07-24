from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from context import Context


class Pokemon(Cog):
    def auto_solve(self, content: str) -> list[str]:
        msg_match = re.match(r'the pokémon is (?P<pokemon>[^"]+).', content.lower())

        if msg_match is None:
            raise commands.BadArgument("Message did not match regex.")

        hint = re.sub(r"\\", "", msg_match.groups()[0])

        found = []

        sorted_guesses = [p for p in self.bot.pokemon if len(p) == len(hint)]
        for p in sorted_guesses:
            results = re.match(hint.replace(r"_", r"[a-z]{1}"), p)

            if results is None:
                continue

            answer = results.group()

            found.append(answer)

        return found

    @commands.Cog.listener("on_message")
    async def on_pokemon(self, message: discord.Message):
        if message.author.id != self.bot.config["ids"]["poketwo_id"]:
            return

        if message.guild is None:
            return

        if str(message.guild.id) not in await self.bot.redis.smembers("poketwo_guilds"):
            return

        try:
            await message.channel.send("\n".join(self.auto_solve(message.content)))
        except commands.BadArgument:
            pass
