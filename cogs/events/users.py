import datetime
import imghdr
from io import BytesIO
from typing import List, Tuple

import discord
from bot import Bot
from discord.ext import commands, tasks


async def setup(bot: Bot):
    await bot.add_cog(UserEvents(bot))


class UserEvents(commands.Cog, name="user_events"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self._usernames: List[Tuple[int, str, datetime.datetime]] = []
        self._discims: List[Tuple[int, str, datetime.datetime]] = []
        self._avatars: List[Tuple[int, bytes, str, datetime.datetime]] = []
        self._statuses: List[Tuple[int, datetime.datetime]] = []

    async def _bulk_insert(self):
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

        if self._statuses:
            sql = """
            INSERT INTO uptime_logs(user_id, time)
            VALUES ($1, $2)
            """
            await self.bot.pool.executemany(sql, self._statuses)
            self._statuses.clear()

    async def cog_unload(self):
        await self._bulk_insert()
        self.bulk_insert.cancel()

    async def cog_load(self) -> None:
        self.bulk_insert.start()

    @tasks.loop(minutes=3.0)
    async def bulk_insert(self):
        await self._bulk_insert()

    @commands.Cog.listener("on_user_update")
    async def on_username_update(self, before: discord.User, after: discord.User):
        if before.name != after.name:
            self._usernames.append((after.id, after.name, discord.utils.utcnow()))

    @commands.Cog.listener("on_user_update")
    async def on_discrim_update(self, before: discord.User, after: discord.User):
        if before.discriminator != after.discriminator:
            self._discims.append(
                (after.id, after.discriminator, discord.utils.utcnow())
            )

    @commands.Cog.listener("on_user_update")
    async def on_avatar_update(self, before: discord.User, after: discord.User):
        if before.avatar != after.avatar:
            try:
                avatar = await after.display_avatar.replace(size=4096).read()
            except discord.NotFound:
                return

            file_type = imghdr.what(None, avatar) or "png"

            self._avatars.append((after.id, avatar, file_type, discord.utils.utcnow()))

    @commands.Cog.listener("on_presence_update")
    async def on_status_update(self, before: discord.Member, after: discord.Member):
        if before.status != after.status:
            self._statuses.append((after.id, discord.utils.utcnow()))
