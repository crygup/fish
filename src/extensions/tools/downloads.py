from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Literal, cast

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import (
    Downloader,
    KlipyUrlConverter,
    TenorUrlConverter,
    is_discord_media_url,
    to_image,
)

if TYPE_CHECKING:
    from extensions.context import Context


class DownloadFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    format: Literal["mp4", "mp3", "webm", "gif"] = commands.flag(
        description="What format to download as (Klipy and Tenor videos are sent as GIFs).",
        default="mp4",
    )
    title: str | None = commands.flag(
        description="The title of the video to save as.",
        default=None,
    )
    ignore_checks: bool = commands.flag(
        description="you cant use this lol", default=False
    )
    hidden: bool = commands.flag(
        description="Hides the download (only for app commands)", default=True
    )


class Downloads(Cog):
    @commands.hybrid_command(
        name="download",
        aliases=("dl",),
        enabled=True,
        extras={"usage": "<url> [-format mp4 -title <name> -hidden]"},
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        url="URL off the internet like Instagram/TikTok/Twitter",
    )
    async def download(self, ctx: Context, url: str, *, flags: DownloadFlags):
        """Download media from a supported website.

        -# -format    Choose MP4, MP3, WEBM, or GIF. Defaults to MP4.
        -# -title     Change the downloaded file name.
        -# -hidden    Hide the response when using the app command.
        """

        if is_discord_media_url(url):
            return

        async with ctx.typing(ephemeral=flags.hidden):
            try:
                url = await TenorUrlConverter().convert(ctx, url)
                img = cast(BytesIO, await to_image(ctx.session, url))
                downloader = Downloader(ctx, url)
                if img.getbuffer().nbytes > downloader.max_filesize:
                    await ctx.send(
                        f"This GIF exceeds {downloader.upload_limit_description}.",
                        ephemeral=flags.hidden,
                    )
                    return
                await ctx.send(
                    file=discord.File(img, filename="tenor.gif"), ephemeral=True
                )

                return

            except commands.BadArgument:
                pass

            # Resolve Klipy pages to their direct media URL before yt-dlp sees
            # them. This avoids sending the Cloudflare-protected HTML page to
            # the generic extractor.
            try:
                url = await KlipyUrlConverter(flags.format).convert(ctx, url)
            except commands.BadArgument:
                pass

            dl = Downloader(
                ctx,
                url,
                format=flags.format,
                filename=flags.title,
            )

            await dl.download()
