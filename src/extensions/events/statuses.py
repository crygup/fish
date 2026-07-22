from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    pass


class StatusCog(Cog):

    _TRACKED_STATUSES = frozenset({"offline", "idle", "online", "dnd"})

    async def store_status(self, member: discord.Member, status: str) -> None:
        sql = """
        INSERT INTO user_statuses (user_id, guild_id, status, last_seen)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (user_id, guild_id, status)
        DO UPDATE SET last_seen = EXCLUDED.last_seen
        """

        await self.bot.pool.execute(sql, member.id, member.guild.id, status)

    @commands.Cog.listener("on_presence_update")
    async def _on_presence_update(self, before: discord.Member, after: discord.Member):
        if self.bot.user and after.id == self.bot.user.id:
            return
        if "status" in self.bot.db_cache.get_opted_out(after.id):
            return

        if before.status == after.status:
            return

        status = str(after.status)
        if status == "invisible":
            status = "offline"
        if status not in self._TRACKED_STATUSES:
            return

        await self.store_status(after, status)
