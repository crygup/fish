from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog
from utils import VIDEOS_RE, download, run, TenorUrlConverter, to_image, TENOR_PAGE_RE

if TYPE_CHECKING:
    from extensions.context import Context


class AutoDownload(Cog):
    cd_mapping = commands.CooldownMapping.from_cooldown(
        1, 5, commands.BucketType.member
    )

    @commands.Cog.listener("on_message")
    async def auto_download(self, message: discord.Message):
        if str(message.channel.id) not in await self.bot.redis.smembers(
            "auto_downloads"
        ):  # type: ignore
            return

        if message.author.bot:
            return

        video_match = VIDEOS_RE.search(message.content)
        tenor_match = TENOR_PAGE_RE.search(message.content)

        if not tenor_match:
            if video_match is None or video_match and video_match.group(0) == "":
                return

        bucket = self.cd_mapping.get_bucket(message)

        if bucket:
            retry_after = bucket.update_rate_limit()

            if retry_after:
                return

        ctx: Context = await self.bot.get_context(message)  # type: ignore

        if tenor_match:
            try:
                url = await TenorUrlConverter().convert(ctx, message.content)
                img = await to_image(ctx.session, url)
                await ctx.send(
                    file=discord.File(img, filename="tenor.gif"), ephemeral=True
                )

                return

            except commands.BadArgument:
                pass

        async with ctx.typing(ephemeral=True):
            filename = await download(message.content, bot=self.bot)

        try:
            file = discord.File(rf"files/downloads/{filename}", f"{filename}")

            await ctx.send(file=file, ephemeral=True)
        except (FileNotFoundError, discord.HTTPException):
            await ctx.send(
                "No file found, maybe file too large or improper URL provided."
            )

        try:
            await run(f"cd files/downloads && rm {filename}")
        except:
            pass
