import os
import re
import secrets
import time
from typing import Dict

import discord
from bot import Bot, Context
from discord.ext import commands, tasks
from typing_extensions import reveal_type
from utils import get_video, video_regexes, to_thread, natural_size
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

    async def download_video(self, video: str, options: Dict):
        with YoutubeDL(options) as ydl:
            ydl.download(video)

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

        try:
            video = await get_video(ctx, ctx.message.content, True)
        except commands.BadArgument as e:
            await ctx.send(str(e))
            return

        if video is None:
            return

        pattern = re.compile(
            r"(https?:\/\/vm.tiktok.com\/[a-zA-Z0-9_-]{9,})|(https?:\/\/(www.)?tiktok.com\/@?[a-zA-Z0-9_]{4,}\/video\/[0-9]{1,})"
        )

        pattern = re.compile(
            r"(https?:\/\/vm.tiktok.com\/[a-zA-Z0-9_-]{9,})|(https?:\/\/(www.)?tiktok.com\/@?[a-zA-Z0-9_]{4,}\/video\/[0-9]{1,})"
        )

        ydl_opts = {
            "format": f"bestvideo+bestaudio[ext=mp4]/best",
            "outtmpl": f"files/videos/{name}.%(ext)s",
            "quiet": True,
            "max_filesize": ctx.guild.filesize_limit,
        }

        if pattern.search(video):
            ydl_opts["format_sort"] = ['vcodec:h264']

        msg = await ctx.send("Downloading video")
        self.current_downloads.append(f"{name}.mp4")

        start = time.perf_counter()
        try:
            await self.download_video(video, ydl_opts)
        except commands.BadArgument as e:
            return await msg.edit(content=str(e))
        stop = time.perf_counter()

        dl_time = f"Took `{round(stop - start, 2)}` seconds to download."

        try:
            _file = discord.File(f"files/videos/{name}.mp4")
            await msg.edit(content=dl_time, attachments=[_file])

        except (ValueError, discord.Forbidden):
            await msg.edit(content="Failed to download, try again later?")

        except (FileNotFoundError, discord.HTTPException):
            await msg.edit(
                content=f"Video file size is too big, try a shorter video. This server's file size limit is **`{natural_size(ctx.guild.filesize_limit)}`**."
            )

        try:
            os.remove(f"files/videos/{name}.mp4")
        except (FileNotFoundError, PermissionError):
            pass

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
