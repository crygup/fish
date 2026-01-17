from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING, Literal

import discord
from discord.ext import commands
from core import Cog
from utils import Downloader, TenorUrlConverter, to_image
from discord import app_commands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class DownloadFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    format: Literal["mp4", "mp3", "webm"] = commands.flag(
        description="What format the video should download as.", default="mp4"
    )
    title: str = commands.flag(
        description="The title of the video to save as.",
        default=secrets.token_urlsafe(8),
    )
    ignore_checks: bool = commands.flag(
        description="you cant use this lol", default=False
    )
    TwitterGif: bool = commands.flag(
        name="twitter_gif",
        description="Converts twitter GIFs into an actual .gif file.",
        default=True,
    )
    hidden: bool = commands.flag(
        description="Hides the download (only for app commands)", default=True
    )


class Downloads(Cog):
    @commands.hybrid_command(name="download", aliases=("dl",), enabled=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def download(self, ctx: Context, url: str, *, flags: DownloadFlags):
        """Download a video off the internet"""
        
        async with ctx.typing(ephemeral=flags.hidden):
            try:
                url = await TenorUrlConverter().convert(ctx, url)
                img = await to_image(ctx.session, url)
                await ctx.send(
                    file=discord.File(img, filename="tenor.gif"), ephemeral=True
                )

                return

            except commands.BadArgument:
                pass

            dl = Downloader(
                ctx,
                url,
                format=flags.format,
                twitterGif=flags.TwitterGif,
                filename=flags.title,
            )

            await dl.download()
