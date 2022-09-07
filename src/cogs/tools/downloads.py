import argparse
import os
import re
import secrets
import shlex
import time
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks
from yt_dlp import YoutubeDL

from bot import Bot, Context
from utils import get_video, natural_size, to_thread

from ._base import CogBase


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class DownloadCommands(CogBase):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.currently_downloading: list[str] = []

    async def cog_unload(self):
        self.delete_videos.cancel()

    async def cog_load(self) -> None:
        self.delete_videos.start()

    @to_thread
    def download_video(self, video: str, options: Dict):
        with YoutubeDL(options) as ydl:
            ydl.download(video)

    @commands.command(name="download", aliases=("dl",))
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def download(self, ctx: Context, url: str, *, flags: Optional[str]):
        """Downloads a video from certain sites.

        Accepted sites are, Youtube, TikTok, Twitter, and reddit."""

        default_name = secrets.token_urlsafe(8)
        default_format = "mp4"
        skip_check = False
        audio = False

        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument("-format", type=str, default=default_format)
        parser.add_argument("-dev", action="store_true")

        if flags is not None:
            try:
                args = parser.parse_args(shlex.split(flags))
            except RuntimeError as e:
                await ctx.send(str(e))
                return

            if args.dev:
                check = await self.bot.is_owner(ctx.author)

                if not check:
                    raise commands.NotOwner

                skip_check = True

            if args.format is not None:
                if not re.match(r"(mp4|webm|mp3)", args.format):
                    await ctx.send("Invalid format.")
                    return

                if re.match(r"(mp3)", args.format):
                    audio = True
                    default_format = args.format
                else:
                    default_format = args.format

        if not skip_check:
            video = await get_video(ctx, url)

            if video is None:
                return await ctx.send("Invalid video url.")
        else:
            video = url

        pattern = re.compile(
            r"https://(www|vt|vm|m).tiktok.com/(@)?[a-zA-Z0-9_-]{3,}(/video/[0-9]{1,})?"
        )

        ydl_opts = {
            "format": f"bestvideo+bestaudio[ext={default_format}]/best"
            if not audio
            else f"bestaudio/best",
            "outtmpl": f"src/files/videos/{default_name}.%(ext)s",
            "quiet": True,
            "max_filesize": ctx.guild.filesize_limit,
        }

        if pattern.search(video):
            ydl_opts["format_sort"] = ["vcodec:h264"]

        if audio:
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]

        message = await ctx.reply("Downloading video")

        self.currently_downloading.append(f"{default_name}.{default_format}")

        start = time.perf_counter()
        await self.download_video(video, ydl_opts)
        stop = time.perf_counter()

        dl_time = (
            f"Took `{round(stop - start, 2)}` seconds to download, {ctx.author.mention}"
        )

        await message.edit(content="Downloaded, uploading...")

        try:
            _file = discord.File(f"src/files/videos/{default_name}.{default_format}")
            await message.edit(content=dl_time, attachments=[_file])

        except (ValueError, discord.Forbidden):
            await message.edit(content="Failed to download, try again later?")

        except (FileNotFoundError, discord.HTTPException):
            await message.edit(
                content=f"Video file size is too big, try a shorter video. This server's file size limit is **`{natural_size(ctx.guild.filesize_limit)}`**."
            )

        try:
            os.remove(f"src/files/videos/{default_name}.{default_format}")
        except (FileNotFoundError, PermissionError):
            pass

        self.currently_downloading.remove(f"{default_name}.{default_format}")

    @tasks.loop(minutes=10.0)
    async def delete_videos(self):
        valid_formats = (
            "mp4",
            "webm",
            "mov",
            "mp3",
            "ogg",
            "wav",
        )
        for file in os.listdir("src/files/videos"):
            if file.endswith(valid_formats):
                if file not in self.currently_downloading:
                    os.remove(f"src/files/videos/{file}")
