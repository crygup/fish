from __future__ import annotations
import datetime

import random
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(UserEvents(bot))


class UserEvents(commands.Cog, name="user_events"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Cog.listener("on_member_update")
    async def on_nickname_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            if after.nick is None:
                return

            sql = """
            INSERT INTO nickname_logs(user_id, guild_id, nickname, created_at)
            VALUES ($1, $2, $3, $4)
            """
            await self.bot.pool.execute(
                sql, after.id, after.guild.id, after.nick, discord.utils.utcnow()
            )

    @commands.Cog.listener("on_user_update")
    async def on_username_update(self, before: discord.User, after: discord.User):
        if before.name != after.name:
            sql = """
            INSERT INTO username_logs(user_id, username, created_at)
            VALUES ($1, $2, $3)
            """
            await self.bot.pool.execute(
                sql, after.id, after.name, datetime.datetime.utcnow()
            )

    @commands.Cog.listener("on_user_update")
    async def on_discrim_update(self, before: discord.User, after: discord.User):
        if before.discriminator != after.discriminator:
            sql = """
            INSERT INTO discrim_logs(user_id, discrim, created_at)
            VALUES ($1, $2, $3)
            """
            await self.bot.pool.execute(
                sql, after.id, after.discriminator, datetime.datetime.utcnow()
            )

    @commands.Cog.listener("on_presence_update")
    async def on_status_update(self, before: discord.Member, after: discord.Member):
        if before.status != after.status:
            results = await self.bot.pool.fetchrow(
                "SELECT user_id FROM uptime_logs WHERE user_id = $1", after.id
            )

            if results is None:
                sql = """
                INSERT INTO uptime_logs(user_id, time)
                VALUES($1, $2)"""
                await self.bot.pool.execute(sql, after.id, discord.utils.utcnow())
                return

            sql = """
            UPDATE uptime_logs
            SET time = $2 WHERE user_id = $1
            """
            await self.bot.pool.execute(sql, after.id, discord.utils.utcnow())
