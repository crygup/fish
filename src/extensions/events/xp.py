from __future__ import annotations

import random
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog
from core.cache import REPUTATION_BONUS_GUILD_ID, REPUTATION_BONUS_USER_ID

if TYPE_CHECKING:
    pass


class XPCog(Cog):
    xp_cd: commands.CooldownMapping[discord.Message]

    # These bonuses are intentionally kept in the XP path instead of the rep
    # command.  That means Fishie and imported Tatsu reputation events grant
    # the same bonus, including when the event happened earlier in the period.
    REPUTATION_USER_ID = REPUTATION_BONUS_USER_ID
    REPUTATION_GUILD_ID = REPUTATION_BONUS_GUILD_ID

    async def add_xp(self, message: discord.Message, amount: int | None = None):
        if amount is None:
            amount = random.randint(10, 20)

        # Reputation events are populated once at startup and updated by the
        # reputation command/listener.  This keeps the message XP hot path
        # free of a database query while allowing user and guild bonuses to
        # stack independently.
        amount += 5 * self.bot.db_cache.reputation_bonus_count(message.author.id)

        sql = """
        INSERT INTO message_xp (user_id, messages, xp) 
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE 
        SET messages = message_xp.messages + 1, 
            xp = message_xp.xp + $3
        WHERE message_xp.user_id = $1
        """

        await self.bot.pool.execute(sql, message.author.id, 1, amount)

    @commands.Cog.listener("on_message")
    async def xp_message(self, message: discord.Message):
        if message.author.bot:
            return
        if self.bot.db_cache.user_tracking_opted_out(message.author.id, "xp"):
            return

        bucket = self.xp_cd.get_bucket(message)
        if bucket:
            retry_after = bucket.update_rate_limit()
            if retry_after:
                return

        await self.add_xp(message)
