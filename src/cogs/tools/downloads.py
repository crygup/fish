from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks

from utils import VideoIsLive, to_thread, download_video

from ._base import CogBase

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class DownloadFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    format: Literal["mp4", "mp3", "webm"] = commands.flag(
        description="What format the video should download as.", default="mp4"
    )


class DownloadCommands(CogBase):
    @commands.hybrid_command(name="download", aliases=("dl",))
    @commands.cooldown(1, 5, commands.BucketType.user)
    @app_commands.describe(
        video="The video URL, this can be Youtube, TikTok, twitter, reddit or instagram"
    )
    async def download(self, ctx: Context, video: str, *, flags: DownloadFlags):
        """Download a video from the internet."""

        dl_format = flags.format
        skip_check = False
        audio = dl_format == "mp3"

        if flags.dev:
            skip_check = await self.bot.is_owner(ctx.author)

        await download_video(video, dl_format, ctx, audio=audio, skip_check=skip_check)
