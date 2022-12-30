from __future__ import annotations

import datetime
import random
from typing import TYPE_CHECKING

import asyncpg
import discord
from discord.ext import commands

from utils import DoNothing

if TYPE_CHECKING:
    from bot import Bot


async def setup(bot: Bot):
    await bot.add_cog(AvatarEvents(bot))


class AvatarEvents(commands.Cog, name="user_events"):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def post_file(
        self,
        file: discord.File,
        webhook: discord.Webhook,
        user: discord.User | discord.Member,
    ) -> discord.Message:
        return await webhook.send(
            f"{user.mention} | {user} | {user.id} | {discord.utils.format_dt(discord.utils.utcnow())}",
            file=file,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def do_avatar(
        self, user: discord.User | discord.Member, asset: discord.Asset
    ) -> discord.Message:
        webhook = random.choice(
            [webhook for _, webhook in self.bot.avatar_webhooks.items()]
        )

        size = 4096
        message = None
        for _ in range(9):
            try:
                avatar = await asset.replace(size=size).to_file()
            except (discord.NotFound, ValueError):
                break

            try:
                message = await self.post_file(file=avatar, webhook=webhook, user=user)
                break
            except discord.HTTPException:
                size //= 2
                continue

        if message is None:
            channel: discord.TextChannel = self.bot.get_channel(1058221675306569748)  # type: ignore
            await channel.send(
                f"<@766953372309127168> Failed to post {user.id}'s avatar."
            )
            raise DoNothing()

        return message

    @commands.Cog.listener("on_user_update")
    async def on_avatar_update(self, before: discord.User, after: discord.User):
        if before.avatar == after.avatar:
            return
        if after.display_avatar.key.isdigit():
            return

        if "avatars" in await self.bot.redis.smembers(f"opted_out:{after.id}"):
            return

        message = await self.do_avatar(user=after, asset=after.display_avatar)

        sql = """
        INSERT INTO avatars(user_id, avatar_key, created_at, avatar)
        VALUES($1, $2, $3, $4)"""

        now = discord.utils.utcnow()
        try:
            await self.bot.pool.execute(
                sql,
                after.id,
                after.display_avatar.key,
                now,
                message.attachments[0].url,
            )
        except asyncpg.UniqueViolationError:
            pass

    @commands.Cog.listener("on_member_update")
    async def on_guild_avatar_update(
        self, before: discord.Member, after: discord.Member
    ):
        if "guild_avatars" in await self.bot.redis.smembers(f"opted_out:{after.id}"):
            return

        if before.guild_avatar == after.guild_avatar:
            return

        if after.guild_avatar is None:
            return

        message = await self.do_avatar(user=after, asset=after.guild_avatar)

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
