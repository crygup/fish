import argparse
from io import BytesIO
import os
import re
import secrets
import shlex
import time
from typing import Dict, List, Optional, Union

import discord
from bot import Bot, Context
from discord.ext import commands, tasks
from utils import (
    GuildContext,
    TenorUrlConverter,
    get_video,
    video_regexes,
    human_join,
    EmojiConverter,
)
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

    @commands.command(name="steal", aliases=("clone",))
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def steal(self, ctx: GuildContext, *, emojis: Optional[str]):
        ref = ctx.message.reference
        content = ctx.message.content

        if emojis is None:
            if ref is None:
                await ctx.send(
                    "You need to provide some emojis to steal, either reply to a message or give them as an argument."
                )
                return

            resolved = ref.resolved
            if isinstance(resolved, discord.DeletedReferencedMessage):
                return

            if resolved is None:
                return

            content = resolved.content

        pattern = re.compile(r"<a?:[a-zA-Z0-9\_]{1,}:[0-9]{1,}>")
        results = pattern.findall(content)

        if len(results) == 0:
            await ctx.send("No emojis found.")
            return

        message = await ctx.send("Stealing emojis...")

        completed_emojis = []
        for result in results:
            emoji = await commands.PartialEmojiConverter().convert(ctx, result)

            if emoji is None:
                continue

            try:
                e = await ctx.guild.create_custom_emoji(
                    name=emoji.name, image=await emoji.read()
                )
                completed_emojis.append(str(e))
            except discord.HTTPException:
                pass

            await message.edit(
                content=f'Successfully stole {human_join(completed_emojis, final="and")} *({len(completed_emojis)}/{len(results)})*.'
            )

    @commands.command(name="tenor")
    async def tenor(self, ctx: commands.Context, url: str):
        """Gets the actual gif URL from a tenor link"""

        real_url = await TenorUrlConverter().convert(ctx, url)
        await ctx.send(f"Here is the real url: {real_url}")

    @commands.command(name="download", aliases=("dl",))
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def download(self, ctx: GuildContext, url: str, *, flags: Optional[str]):
        """Downloads a video from certain sites.

        Accepted sites are, Youtube, TikTok, Twitter, and reddit."""

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
            pattern = re.compile(
                r"(https?:\/\/vm.tiktok.com\/[a-zA-Z0-9_-]{9,})|(https?:\/\/(www.)?tiktok.com\/@?[a-zA-Z0-9_]{4,}\/video\/[0-9]{1,})"
            )
            video_format = (
                "-S vcodec:h264"
                if pattern.search(video)
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
            ydl.download(video)
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

    @commands.group(name="emoji", invoke_without_command=True)
    async def emoji(
        self,
        ctx: Context,
        emoji: Optional[Union[discord.Emoji, discord.PartialEmoji, str]],
    ):
        """Gets information about an emoji."""

        if ctx.message.reference is None and emoji is None:
            raise commands.BadArgument("No emoji provided.")

        if isinstance(emoji, str):
            _emoji = await ctx.get_twemoji(str(emoji))

            if _emoji is None:
                raise commands.BadArgument("No emoji found.")

            await ctx.send(file=discord.File(BytesIO(_emoji), filename=f"emoji.png"))
            return

        if emoji:
            emoji = emoji

        else:
            if not ctx.message.reference:
                raise commands.BadArgument("No emoji provided.")

            reference = ctx.message.reference.resolved

            if (
                isinstance(reference, discord.DeletedReferencedMessage)
                or reference is None
            ):
                raise commands.BadArgument("No emoji found.")

            emoji = (await EmojiConverter().from_message(ctx, reference.content))[0]

        if emoji is None:
            raise TypeError("No emoji found.")

        embed = discord.Embed(timestamp=emoji.created_at, title=emoji.name)

        if isinstance(emoji, discord.Emoji) and emoji.guild:
            if not emoji.available:
                embed.title = f"~~{emoji.name}~~"

            embed.add_field(
                name="Guild",
                value=f"{ctx.bot.e_replies}{str(emoji.guild)}\n"
                f"{ctx.bot.e_reply}{emoji.guild.id}",
            )

            femoji = await emoji.guild.fetch_emoji(emoji.id)
            if femoji.user:
                embed.add_field(
                    name="Created by",
                    value=f"{ctx.bot.e_replies}{str(femoji.user)}\n"
                    f"{ctx.bot.e_reply}{femoji.user.id}",
                )

        embed.add_field(
            name="Raw text", value=f"`<:{emoji.name}\u200b:{emoji.id}>`", inline=False
        )

        embed.set_footer(text=f"ID: {emoji.id} \nCreated at")
        embed.set_image(url=f'attachment://emoji.{"gif" if emoji.animated else "png"}')
        file = await emoji.to_file(
            filename=f'emoji.{"gif" if emoji.animated else "png"}'
        )
        await ctx.send(embed=embed, file=file)

    @emoji.command(name="rename")
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_rename(self, ctx: Context, emoji: discord.Emoji, *, name: str):
        pattern = re.compile(r"[a-zA-Z0-9_ ]")
        if not pattern.match(name):
            raise commands.BadArgument(
                "Name can only contain letters, numbers, and underscores."
            )

        await emoji.edit(name=name)
        await ctx.send(f"Renamed {emoji} to **`{name}`**.")

    @emoji.command(name="delete")
    @commands.has_guild_permissions(manage_emojis=True)
    @commands.bot_has_guild_permissions(manage_emojis=True)
    async def emoji_delete(self, ctx: Context, *emojis: discord.Emoji):
        value = await ctx.prompt(
            f"Are you sure you want to delete {len(emojis):,} emoji{'s' if len(emojis) > 1 else ''}?"
        )
        if not value:
            await ctx.send(
                f"Well I didn't want to delete {'them' if len(emojis) > 1 else 'it'} anyway."
            )
            return

        message = await ctx.send(
            f"Deleting {len(emojis):,} emoji{'s' if len(emojis) > 1 else ''}..."
        )
        deleted_emojis = []

        for emoji in emojis:
            deleted_emojis.append(f"`{emoji}`")
            await emoji.delete()
            await message.edit(
                content=f"Successfully deleted {human_join(deleted_emojis, final='and')} *({len(deleted_emojis)}/{len(emojis)})*."
            )

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
        name="set", aliases=("create", "create_channel", "create_dl_channel")
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

    @auto_download.command(name="remove", aliases=("delete",))
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
