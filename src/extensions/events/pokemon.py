from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from core import Cog, is_operational_guild
from core.handoff import is_legacy_instance

if TYPE_CHECKING:
    pass

POKETWO_ID = 716390085896962058


class Pokemon(Cog):

    async def _log_solve(
        self,
        user_id: int,
        pokemon_name: str,
        method: str,
        guild_id: Optional[int] = None,
    ) -> None:
        if is_legacy_instance(self.bot):
            return
        if self.bot.db_cache.user_tracking_opted_out(user_id, "pokemon"):
            return
        sql = """
        INSERT INTO pokemon_solves (user_id, pokemon_name, method, guild_id)
        VALUES ($1, $2, $3, $4)
        """
        await self.bot.pool.execute(sql, user_id, pokemon_name, method, guild_id)

    async def _find_hint_requester(
        self, channel: discord.abc.Messageable, before: discord.Message
    ) -> Optional[discord.User | discord.Member]:
        try:
            async for msg in channel.history(limit=5, before=before):
                if msg.content.lower().startswith(f"<@{POKETWO_ID}> h"):
                    return msg.author
        except discord.HTTPException:
            pass

        return None

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
        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(message):
            return
        if message.author.id != POKETWO_ID:
            return

        if message.guild is None:
            return

        if message.guild.id not in self.bot.db_cache.poketwo_guilds:
            return
        target_channel = self.bot.db_cache.poketwo_channels.get(message.guild.id)
        if target_channel is not None and message.channel.id != target_channel:
            return

        try:
            found = self.auto_solve(message.content)
        except commands.BadArgument:
            return

        if not found:
            return

        requester = await self._find_hint_requester(message.channel, message)

        if requester:
            for name in found:
                await self._log_solve(
                    requester.id, name, "auto_solve", message.guild.id
                )

        await message.channel.send("\n".join(found))
