import datetime
from typing import List, Tuple

import discord
from bot import Bot
from discord.ext import commands, tasks


async def setup(bot: Bot):
    await bot.add_cog(GuildEvents(bot))


class GuildEvents(commands.Cog, name="guild_events"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._joins: List[Tuple[int, int, datetime.datetime]] = []

    async def _bulk_insert(self):
        if self._joins:
            sql = """
            INSERT INTO member_join_logs(member_id, guild_id, time)
            VALUES ($1, $2, $2)
            """
            await self.bot.pool.executemany(sql, self._joins)
            self._joins.clear()

    async def cog_unload(self):
        await self._bulk_insert()
        self.bulk_insert.cancel()

    async def cog_load(self) -> None:
        self.bulk_insert.start()

    @tasks.loop(minutes=3.0)
    async def bulk_insert(self):
        await self._bulk_insert()

    @commands.Cog.listener("on_member_join")
    async def on_member_join(self, member: discord.Member):
        self._joins.append((member.id, member.guild.id, datetime.datetime.now()))
