import datetime
import imghdr
import random
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

            try:
                avatar = await after.guild_avatar.replace(size=4096).to_file()
            except discord.NotFound:
                return

            webhook = random.choice(
                [webhook for _, webhook in self.bot.avatar_webhooks.items()]
            )

            message = await webhook.send(
                f"{after.mention} | {after} | {after.id} | {discord.utils.format_dt(discord.utils.utcnow())}",
                file=avatar,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

            sql = """
            INSERT INTO guild_avatars(member_id, guild_id, avatar_key, created_at, avatar)
            VALUES($1, $2, $3, $4, $5)"""

            now = discord.utils.utcnow()

            await self.bot.pool.execute(
                sql,
                after.id,
                after.guild.id,
                after.guild_avatar.key,
                now,
                message.attachments[0].url,
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
