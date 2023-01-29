from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from utils import DoNothing, download_video

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(AutoDownloads(bot))


class AutoDownloads(commands.Cog, name="auto_downloads"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.cd_mapping = commands.CooldownMapping.from_cooldown(
            1, 5, commands.BucketType.member
        )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        blocked = await self.bot.redis.smembers("block_list")
        if (
            message.guild is None
            or message.author.bot
            or isinstance(message.guild.me, discord.Member)
            and not message.channel.permissions_for(message.guild.me).send_messages
            or message.author.id == message.guild.me.id
            or not str(message.channel.id)
            in await self.bot.redis.smembers("auto_download_channels")
            or str(message.author.id) in blocked
            or str(message.guild.owner_id) in blocked
            or str(message.guild.id) in blocked
        ):
            return

        bucket = self.cd_mapping.get_bucket(message)

        if bucket is None:
            raise DoNothing()

        retry_after = bucket.update_rate_limit()

        if retry_after:
            raise commands.CommandOnCooldown(
                cooldown=self.cd_mapping,  # type: ignore
                retry_after=retry_after,
                type=commands.BucketType.member,
            )

        ctx: Context = await self.bot.get_context(message)

        if ctx is None or not isinstance(ctx.author, discord.Member):
            return

        await download_video(ctx.message.content, "mp4", ctx)
