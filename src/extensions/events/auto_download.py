from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from core import Cog
from utils import (
    KLIPY_RE,
    TENOR_PAGE_RE,
    VIDEOS_RE,
    Downloader,
    KlipyUrlConverter,
    TenorUrlConverter,
    to_image,
)

if TYPE_CHECKING:
    from extensions.context import Context


class AutoDownload(Cog):
    cd_mapping = commands.CooldownMapping.from_cooldown(
        1, 5, commands.BucketType.member
    )

    @commands.Cog.listener("on_message")
    async def auto_download(self, message: discord.Message):
        if message.channel.id not in self.bot.db_cache.auto_downloads:
            return

        if message.author.bot:
            return

        video_match = VIDEOS_RE.search(message.content)
        tenor_match = TENOR_PAGE_RE.search(message.content)
        klipy_match = KLIPY_RE.search(message.content)
        has_video_match = bool(video_match and video_match.group(0))

        if not tenor_match and not klipy_match and not has_video_match:
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
                img = cast(BytesIO, await to_image(ctx.session, url))
                downloader = Downloader(ctx, url)
                if img.getbuffer().nbytes > downloader.max_filesize:
                    await ctx.send(
                        f"This GIF exceeds {downloader.upload_limit_description}.",
                        ephemeral=True,
                    )
                    return
                await ctx.send(
                    file=discord.File(img, filename="tenor.gif"), ephemeral=True
                )

                return

            except commands.BadArgument:
                pass

        if klipy_match:
            try:
                url = await KlipyUrlConverter().convert(ctx, klipy_match.group(0))
                async with ctx.typing(ephemeral=True):
                    await Downloader(ctx, url, format="gif").download()
                return
            except commands.BadArgument:
                pass

        async with ctx.typing(ephemeral=True):
            if not video_match or not video_match.group(0):
                return
            dl = Downloader(ctx, video_match.group(0))

            await dl.download()
