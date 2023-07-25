from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING, Literal

import discord
from discord.ext import commands

from core import Cog
from utils import download

if TYPE_CHECKING:
    from core import Fishie


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


class Downloads(Cog):
    @commands.hybrid_command(name="download", aliases=("dl",))
    async def download(
        self, ctx: commands.Context[Fishie], url: str, *, flags: DownloadFlags
    ):
        """Download a video off the internet"""
        async with ctx.typing(ephemeral=True):
            filename = await download(url, flags.format, bot=self.bot)

        file = discord.File(
            rf"files\downloads\{filename}", f"{flags.title}.{flags.format}"
        )

        await ctx.send(file=file, ephemeral=True)

        try:
            os.remove(rf"files\downloads\{filename}")
        except:
            pass
