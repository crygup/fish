from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from extensions.context import Context


class StatusCog(Cog):

    async def store_status(self, member: discord.Member, status: str) -> None:
        sql = """
        INSERT INTO user_statuses (user_id, guild_id, status, last_seen)
        VALUES ($1, $2, $3, now() at time zone 'utc')
        ON CONFLICT (user_id, guild_id, status)
        DO UPDATE SET last_seen = EXCLUDED.last_seen
        """

        await self.bot.pool.execute(sql, member.id, member.guild.id, status)

    @commands.Cog.listener("on_presence_update")
    async def _on_presence_update(self, before: discord.Member, after: discord.Member):
        if self.bot.user and after.id == self.bot.user.id:
            return

        if before.status == after.status:
            return

        await self.store_status(after, str(after.status))
