import os
import re
import secrets
import time
from typing import Dict

import discord
from bot import Bot, Context
from discord.ext import commands, tasks
from typing_extensions import reveal_type
from utils import get_video, regexes
from yt_dlp import YoutubeDL


async def setup(bot: Bot):
    await bot.add_cog(AutoDownloads(bot))


class AutoDownloads(commands.Cog, name="auto_downloads"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.current_downloads = []
        self.cd_mapping = commands.CooldownMapping.from_cooldown(
            10, 10, commands.BucketType.member
        )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if (
            message.guild is None
            or message.author.bot
            or isinstance(message.guild.me, discord.Member)
            and not message.channel.permissions_for(message.guild.me).send_messages
            or message.author.id == message.guild.me.id
            or not str(message.channel.id)
            in await self.bot.redis.smembers("auto_download_channels")
        ):
            return

        ctx: Context = await self.bot.get_context(message)

        if ctx is None or not isinstance(ctx.author, discord.Member):
            return

        name = secrets.token_urlsafe(8)
        video = await get_video(ctx, ctx.message.content, True)

        if video is None:
            return

        video_format = (
            "-S vcodec:h264"
            if re.fullmatch(regexes["VMtiktok"]["regex"], video)
            or re.fullmatch(regexes["WEBtiktok"]["regex"], video)
            else f"bestvideo+bestaudio[ext=mp4]/best"
        )

        def length_check(info: Dict, *, incomplete):
            duration = info.get("duration")
            if duration and duration > 600:
                raise commands.BadArgument(
                    "Video is too long, please keep it under 10 minutes."
                )

        ydl_opts = {
            "format": video_format,
            "outtmpl": f"files/videos/{name}.%(ext)s",
            "match_filter": length_check,
            "quiet": True,
        }

        msg = await ctx.send("Downloading video")
        self.current_downloads.append(f"{name}.mp4")

        start = time.perf_counter()
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download(video)
        except commands.BadArgument as e:
            await msg.edit(content=str(e))
            return

        stop = time.perf_counter()

        dl_time = f"Took `{round(stop - start, 2)}` seconds to download."

        try:
            _file = discord.File(f"files/videos/{name}.mp4")
            await msg.edit(content=dl_time, attachments=[_file])
            try:
                os.remove(f"files/videos/{name}.mp4")
            except (FileNotFoundError, PermissionError):
                pass

        except (ValueError, discord.Forbidden):
            await msg.edit(content="Failed to download, try again later?")

        self.current_downloads.remove(f"{name}.mp4")

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
                if file not in self.current_downloads:
                    os.remove(f"files/videos/{file}")
