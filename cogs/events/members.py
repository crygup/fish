import datetime
import imghdr
from typing import List, Tuple

import discord
from bot import Bot
from discord.ext import commands, tasks


async def setup(bot: Bot):
    await bot.add_cog(MemberEvents(bot))


class MemberEvents(commands.Cog, name="member_events"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Cog.listener("on_member_update")
    async def on_guild_avatar_update(
        self, before: discord.Member, after: discord.Member
    ):
        if before.guild_avatar != after.guild_avatar:
            if after.guild_avatar is None:
                return

            avatar = await after.guild_avatar.replace(size=4096).read()
            sql = """
            INSERT INTO guild_avatar_logs(user_id, avatar, format, created_at, guild_id)
            VALUES ($1, $2, $3, $4, $5)
            """
            await self.bot.pool.execute(
                sql,
                after.id,
                avatar,
                imghdr.what(None, avatar),
                discord.utils.utcnow(),
                after.guild.id,
            )

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
