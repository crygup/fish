import imghdr
from io import BytesIO

import discord
from bot import Bot
from discord.ext import commands


async def setup(bot: Bot):
    await bot.add_cog(UserEvents(bot))


class UserEvents(commands.Cog, name="user_events"):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Cog.listener("on_user_update")
    async def on_username_update(self, before: discord.User, after: discord.User):
        if before.name != after.name:
            sql = """
            INSERT INTO username_logs(user_id, username, created_at)
            VALUES ($1, $2, $3)
            """
            await self.bot.pool.execute(
                sql, after.id, after.name, discord.utils.utcnow()
            )

    @commands.Cog.listener("on_user_update")
    async def on_discrim_update(self, before: discord.User, after: discord.User):
        if before.discriminator != after.discriminator:
            sql = """
            INSERT INTO discrim_logs(user_id, discrim, created_at)
            VALUES ($1, $2, $3)
            """
            await self.bot.pool.execute(
                sql, after.id, after.discriminator, discord.utils.utcnow()
            )

    @commands.Cog.listener("on_user_update")
    async def on_avatar_update(self, before: discord.User, after: discord.User):
        if before.avatar != after.avatar:
            avatar = await after.display_avatar.replace(size=4096).read()
            sql = """
            INSERT INTO avatar_logs(user_id, avatar, format, created_at)
            VALUES ($1, $2, $3, $4)
            """
            await self.bot.pool.execute(
                sql, after.id, avatar, imghdr.what(None, avatar), discord.utils.utcnow()
            )
