import argparse
import os
import re
import secrets
import shlex
import time
from typing import Dict, Optional

import discord
from bot import Bot, Context
from discord.ext import commands, tasks
from utils import get_video, natural_size, to_thread
from yt_dlp import YoutubeDL

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
            os.remove(f"files/videos/{default_name}.{default_format}")
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
        for file in os.listdir("files/videos"):
            if file.endswith(valid_formats):
                if file not in self.currently_downloading:
                    os.remove(f"src/files/videos/{file}")

    @commands.group(
        name="auto_download", aliases=("auto_dl", "adl"), invoke_without_command=True
    )
    async def auto_download(self, ctx: Context):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        results = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not results:
            message = "This server does not have an auto-download channel set yet."

            if (
                isinstance(ctx.author, discord.Member)
                and ctx.author.guild_permissions.manage_guild
            ):
                message += f"\nYou can set one with `{ctx.prefix}auto_download set`."

            await ctx.send(message)
            return

        channel = self.bot.get_channel(results)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        await ctx.send(f"Auto-download is set to {channel.mention}.")

    @auto_download.command(
        name="set",
        aliases=("create", "create_channel", "create_dl_channel"),
        extras={"UPerms": ["Manage Emojis"]},
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_download_set(
        self, ctx: Context, channel: Optional[discord.TextChannel]
    ):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        result = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if result is not None:
            await ctx.send(f"Auto-download is already setup here.")
            return

        if channel is None:
            if not ctx.me.guild_permissions.manage_channels:
                await ctx.send(
                    f"I cannot create a channel so you can either make one yourself or use `{ctx.prefix}auto_download set <channel>` to set an already made one."
                )
                return

            response = await ctx.prompt(
                "You didn't provide a channel so I will create one, is this okay?"
            )
            if response is None:
                await ctx.send(
                    f"Okay, I won't create a channel, instead specify one with `{ctx.prefix}auto_download set <channel>`."
                )
                return

            channel = await ctx.guild.create_text_channel(
                name=f"auto-download",
                topic="Valid links posted here will be auto downloaded. \nAccepted sites are, Youtube, TikTok, Twitter, and reddit.",
            )

            first_sql = """SELECT guild_id FROM guild_settings WHERE guild_id = $1"""
            results = await self.bot.pool.fetchval(first_sql, ctx.guild.id)

            sql = (
                """INSERT INTO guild_settings (guild_id, auto_download) VALUES ($1, $2)"""
                if not results
                else """UPDATE guild_settings SET auto_download = $2 WHERE guild_id = $1"""
            )

            await self.bot.pool.execute(sql, ctx.guild.id, channel.id)
            await self.bot.redis.sadd("auto_download_channels", channel.id)
            await ctx.send(f"Auto-download is now set to {channel.mention}.")
            return

        if not channel.permissions_for(ctx.me).send_messages:
            await ctx.send(
                f"I don't have permission to send messages in {channel.mention}."
            )
            return

        first_sql = """SELECT guild_id FROM guild_settings WHERE guild_id = $1"""
        results = await self.bot.pool.fetchval(first_sql, ctx.guild.id)

        sql = (
            """INSERT INTO guild_settings (guild_id, auto_download) VALUES ($1, $2)"""
            if not results
            else """UPDATE guild_settings SET auto_download = $2 WHERE guild_id = $1"""
        )

        await self.bot.pool.execute(sql, ctx.guild.id, channel.id)
        await self.bot.redis.sadd("auto_download_channels", channel.id)
        await ctx.send(f"Auto-download is now set to {channel.mention}.")

    @auto_download.command(
        name="remove", aliases=("delete",), extras={"UPerms": ["Manage Emojis"]}
    )
    @commands.has_guild_permissions(manage_guild=True)
    async def auto_download_remove(self, ctx: Context):
        sql = """SELECT auto_download FROM guild_settings WHERE guild_id = $1"""
        result = await self.bot.pool.fetchval(sql, ctx.guild.id)

        if not result:
            await ctx.send(
                "This server does not have an auto-download channel set yet."
            )
            return

        if not isinstance(result, int):
            return

        channel = self.bot.get_channel(result)
        if isinstance(channel, discord.TextChannel):
            results = await ctx.prompt(
                f"Are you sure you want to delete {channel.mention}?"
            )
            if not results:
                await ctx.send(f"Well I didn't want to delete it anyway.")
                return

        sql = """UPDATE guild_settings SET auto_download = NULL WHERE guild_id = $1"""
        await self.bot.pool.execute(sql, ctx.guild.id)
        await self.bot.redis.srem("auto_download_channels", result)
        await ctx.send(f"Removed auto-downloads for this server.")
