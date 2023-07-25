from __future__ import annotations

import base64
import os
import random
import re
from typing import TYPE_CHECKING, Any, Dict

import asyncpg
import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class XPCog(Cog):
    xp_cd: commands.CooldownMapping[discord.Message]

    async def add_xp(
        self, message: discord.Message, amount: int = random.randint(10, 20)
    ):
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

        bucket = self.xp_cd.get_bucket(message)
        if bucket:
            retry_after = bucket.update_rate_limit()
            if retry_after:
                return

        await self.add_xp(message)
