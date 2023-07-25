from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog
from utils import VIDEOS_RE, download

if TYPE_CHECKING:
    from core import Fishie


class AutoDownload(Cog):
    cd_mapping = commands.CooldownMapping.from_cooldown(
        1, 5, commands.BucketType.member
    )

    @commands.Cog.listener("on_message")
    async def auto_download(self, message: discord.Message):
        if str(message.channel.id) not in await self.bot.redis.smembers(
            "auto_downloads"
        ):
            return

        if message.author.bot:
            return

        video_match = VIDEOS_RE.search(message.content)

        if video_match is None or video_match and video_match.group(0) == "":
            return

        bucket = self.cd_mapping.get_bucket(message)

        if bucket:
            retry_after = bucket.update_rate_limit()

            if retry_after:
                return

        ctx = await self.bot.get_context(message)

        async with ctx.typing(ephemeral=True):
            filename = await download(message.content, bot=self.bot)

        file = discord.File(rf"files\downloads\{filename}", f"{filename}")

        await ctx.send(file=file, ephemeral=True)

        try:
            os.remove(rf"files\downloads\{filename}")
        except:
            pass
