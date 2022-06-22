import datetime
import imghdr
from io import BytesIO
from typing import List, Tuple

import discord
from bot import Bot
from discord.ext import commands,tasks


async def setup(bot: Bot):
    await bot.add_cog(UserEvents(bot))


class UserEvents(commands.Cog, name="user_events"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._usernames: List[Tuple[int, str, datetime.datetime]] = []
        self._discims: List[Tuple[int, str, datetime.datetime]] = []
        self._avatars: List[Tuple[int, bytes, str, datetime.datetime]] = []
        self.bulk_insert.start()

    def cog_unload(self):
        self.bulk_insert.cancel()

    @tasks.loop(minutes=5.0)
    async def bulk_insert(self):
        if self._usernames:
            sql = """
            INSERT INTO username_logs(user_id, username, created_at)
            VALUES ($1, $2, $3)
            """
            await self.bot.pool.executemany(sql, self._usernames)
            self._usernames.clear()

        if self._discims:
            sql = """
            INSERT INTO discrim_logs(user_id, discrim, created_at)
            VALUES ($1, $2, $3)
            """
            await self.bot.pool.executemany(sql, self._discims)
            self._discims.clear()

        if self._avatars:
            sql = """
            INSERT INTO avatar_logs(user_id, avatar, format, created_at)
            VALUES ($1, $2, $3, $4)
            """
            await self.bot.pool.executemany(sql, self._avatars)
            self._avatars.clear()

    @commands.Cog.listener("on_user_update")
    async def on_username_update(self, before: discord.User, after: discord.User):
        if before.name != after.name:
            self._usernames.append((after.id, after.name, discord.utils.utcnow()))

    @commands.Cog.listener("on_user_update")
    async def on_discrim_update(self, before: discord.User, after: discord.User):
        if before.discriminator != after.discriminator:
            self._discims.append((after.id, after.discriminator, discord.utils.utcnow()))

    @commands.Cog.listener("on_user_update")
    async def on_avatar_update(self, before: discord.User, after: discord.User):
        if before.avatar != after.avatar:
            avatar = await after.display_avatar.replace(size=4096).read()
            file_type = imghdr.what(None, avatar) or 'png'

            self._avatars.append((after.id, avatar, file_type, discord.utils.utcnow()))