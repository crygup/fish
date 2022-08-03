import argparse
import os
import re
import secrets
import shlex
import time
from typing import Dict, List, Optional

import discord
from bot import Bot
from discord.ext import commands, tasks
from utils import GuildContext, TenorUrlConverter, get_video, regexes, human_join
from yt_dlp import YoutubeDL


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class Tools(commands.Cog, name="tools"):
    """Useful tools"""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.currently_downloading: list[str] = []

    async def cog_unload(self):
        self.delete_videos.cancel()

    async def cog_load(self) -> None:
        self.delete_videos.start()

    @commands.command(name="tenor")
    async def tenor(self, ctx: commands.Context, url: str):
        """Gets the actual gif URL from a tenor link"""

        real_url = await TenorUrlConverter().convert(ctx, url)
        await ctx.send(f"Here is the real url: {real_url}")

    @commands.command(name="download", aliases=("dl",))
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def download(self, ctx: GuildContext, url: str, *, flags: Optional[str]):
        """Downloads a video from certain sites.

        Accepted sites are, Youtube, TikTok, Twitter, Twitch, and reddit."""

        default_name = secrets.token_urlsafe(8)
        default_format = "mp4"
        audio_only = False
        check_channel = True
        valid_video_formats = [
            "mp4",
            "webm",
            "mov",
        ]
        valid_audio_formats = [
            "mp3",
            "ogg",
            "wav",
        ]

        if flags:
            parser = Arguments(add_help=False, allow_abbrev=False)
            parser.add_argument("-dev", action="store_true")
            parser.add_argument("-format", type=str)

            try:
                _flags = parser.parse_args(shlex.split(flags))
            except Exception as e:
                return await ctx.send(str(e))

            if _flags.format:
                if _flags.format not in valid_video_formats + valid_audio_formats:
                    return await ctx.send("Invalid format")

                if _flags.format in valid_audio_formats:
                    audio_only = True

                default_format = _flags.format

            if _flags.dev:
                check_channel = False if ctx.author.id == self.bot.owner_id else True

        if check_channel:
            video = await get_video(ctx, url)

            if video is None:
                return await ctx.send("Invalid video url.")

        else:
            video = url

        if audio_only:
            video_format = f"-i --extract-audio --audio-format {default_format}"
        else:
            # tiktok uses h264 encoding so we have to use this
            # in the future i will add more checks to if this is reaccuring issue with other platforms
            # but for now ternary is fine
            video_format = (
                "-S vcodec:h264"
                if re.fullmatch(regexes["VMtiktok"]["regex"], video)
                or re.fullmatch(regexes["WEBtiktok"]["regex"], video)
                else f"bestvideo+bestaudio[ext={default_format}]/best"
            )

        def length_check(info: Dict, *, incomplete):
            duration = info.get("duration")
            if duration and duration > 600:
                raise commands.BadArgument(
                    "Video is too long to download, please keep it under 10 minutes."
                )

        ydl_opts = {
            "format": video_format,
            "outtmpl": f"files/videos/{default_name}.%(ext)s",
            "match_filter": length_check,
            "quiet": True,
        }

        message = await ctx.send("Downloading video")

        self.currently_downloading.append(f"{default_name}.{default_format}")

        start = time.perf_counter()
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download(url)
        stop = time.perf_counter()

        dl_time = f"Took `{round(stop - start, 2)}` seconds to download."

        await message.edit(content="Downloaded, uploading...")

        try:
            _file = discord.File(f"files/videos/{default_name}.{default_format}")
            await message.edit(content=dl_time, attachments=[_file])
            try:
                os.remove(f"files/videos/{default_name}.{default_format}")
            except (FileNotFoundError, PermissionError):
                pass

        except (ValueError, discord.Forbidden):
            await message.edit(content="Failed to download, try again later?")

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
        for file in os.listdir("files/videos"):
            if file.endswith(valid_formats):
                if file not in self.currently_downloading:
                    os.remove(f"files/videos/{file}")
