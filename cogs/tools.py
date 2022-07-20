import argparse
import imghdr
import os
import re
import secrets
import shlex
import textwrap
import time
from io import BytesIO
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


class MyHelp(commands.HelpCommand):
    context: GuildContext

    async def send_bot_help(self, mapping: Dict[commands.Cog, List[commands.Command]]):
        bot = self.context.bot
        ctx = self.context
        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{bot.user.name} help", icon_url=bot.user.display_avatar.url
        )
        for cog, commands in mapping.items():
            if filtered_commands := await self.filter_commands(commands):
                if len(commands) == 0:
                    continue

                if cog is None:
                    continue

                embed.add_field(
                    name=cog.qualified_name.capitalize(),
                    value=human_join(
                        [
                            f"**`{command.qualified_name}`**"
                            for command in cog.get_commands()
                        ],
                        final="and",
                    )
                    or "No commands found here.",
                    inline=False,
                )

        await ctx.send(embed=embed)

    async def send_command_help(self, command: commands.Command):
        bot = self.context.bot
        ctx = self.context

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{command.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = f"```{command.help}```" or "No help yet..."

        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join(
                    [f"**`{alias}`**" for alias in command.aliases], final="and"
                ),
                inline=False,
            )

        if command.cooldown:
            cd = command.cooldown
            embed.add_field(
                name="Cooldown",
                value=f"{cd.rate:,} command every {round(cd.per)} seconds",
            )

        await ctx.send(embed=embed)

    async def send_group_help(self, group: commands.Group):
        bot = self.context.bot
        ctx = self.context

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{group.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = f"```{group.help}```" or "No help yet..."

        if group.commands:
            embed.add_field(
                name="Commands",
                value=human_join(
                    [f"**`{command.name}`**" for command in group.commands], final="and"
                ),
                inline=False,
            )

        if group.aliases:
            embed.add_field(
                name="Aliases",
                value=human_join(
                    [f"**`{alias}`**" for alias in group.aliases], final="and"
                ),
                inline=False,
            )

        if group.cooldown:
            cd = group.cooldown
            embed.add_field(
                name="Cooldown",
                value=f"{cd.rate:,} command every {round(cd.per)} seconds",
            )

        await ctx.send(embed=embed)

    async def send_cog_help(self, cog: commands.Cog):
        ctx = self.context
        bot = ctx.bot

        if bot.user is None:
            return

        embed = discord.Embed(color=0xFAA0C1)
        embed.set_author(
            name=f"{cog.qualified_name.capitalize()} help",
            icon_url=bot.user.display_avatar.url,
        )

        embed.description = f"```{cog.description}```" or "No help yet..."

        commands = await self.filter_commands(cog.get_commands())

        if commands:
            embed.add_field(
                name="Commands",
                value=human_join(
                    [f"**`{command.name}`**" for command in cog.get_commands()],
                    final="and",
                ),
                inline=False,
            )

        if hasattr(cog, "aliases"):
            embed.add_field(name="Aliases", value=human_join([f"**`{alias}`**" for alias in cog.aliases], final="and"), inline=False)  # type: ignore

        await ctx.send(embed=embed)

    async def send_error_message(self, error: commands.CommandError):
        ctx = self.context
        bot = ctx.bot

        if bot.user is None:
            return

        pattern = re.compile(r'No command called "(?P<name>[a-zA-Z0-9]{1,25})" found.')
        results = pattern.match(str(error))

        if results:
            error_command_name = results.group("name").lower()

            for name, cog in bot.cogs.items():
                if error_command_name == cog.qualified_name:
                    await self.send_cog_help(cog)
                    return

                if hasattr(cog, "aliases"):
                    if error_command_name in cog.aliases:  # type: ignore
                        _cog = bot.get_cog(cog.qualified_name)

                        if _cog is None:
                            continue

                        await self.send_cog_help(_cog)
                        return

        else:
            await ctx.send(str(error))


class Tools(commands.Cog, name="tools"):
    """Useful tools"""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._og_help = commands.DefaultHelpCommand()
        self.bot.help_command = MyHelp()
        self.currently_downloading: list[str] = []

    async def cog_unload(self):
        self.bot.help_command = self._og_help
        self.delete_videos.cancel()

    async def cog_load(self) -> None:
        self.bot.help_command = MyHelp()
        self.delete_videos.start()

    @commands.command(name="snipe")
    @commands.is_owner()
    async def snipe(
        self,
        ctx: commands.Context,
        index: Optional[int] = 1,
        channel: Optional[discord.TextChannel] = commands.CurrentChannel,
        *,
        member: Optional[discord.Member] = None,
    ):
        """Shows a deleted message"""
        index = index or 1

        if ctx.guild is None or channel is None:
            return

        await self.bot.get_cog("message_event")._bulk_insert()  # type: ignore

        if member:
            sql = """
            SELECT * FROM message_logs where channel_id = $1 AND author_id = $2 AND deleted IS TRUE ORDER BY created_at DESC
            """
            results = await self.bot.pool.fetch(sql, channel.id, member.id)
        else:
            sql = """
            SELECT * FROM message_logs where channel_id = $1 AND deleted IS TRUE ORDER BY created_at DESC
            """
            results = await self.bot.pool.fetch(sql, channel.id)

        if index - 1 >= len(results):
            await ctx.send("Index out of range.")
            return

        if results == []:
            await ctx.send("Nothing was deleted here...")
            return

        user = self.bot.get_user(results[index - 1]["author_id"]) or "Unknown"

        embeds: List[discord.Embed] = []
        files: List[discord.File] = []

        embed = discord.Embed(
            color=self.bot.embedcolor, timestamp=results[index - 1]["created_at"]
        )
        embed.description = (
            textwrap.shorten(
                results[index - 1]["message_content"], width=300, placeholder="..."
            )
            or "Message did not contain any content."
        )
        embed.set_author(
            name=f"{str(user)}",
            icon_url=user.display_avatar.url
            if isinstance(user, discord.User)
            else ctx.guild.me.display_avatar.url,
        )
        message_id = results[index - 1]["message_id"]
        embed.set_footer(text=f"Index {index} of {len(results)}\nMessage deleted ")
        embeds.append(embed)

        if results[index - 1]["has_attachments"]:
            attachment_sql = """SELECT * FROM message_attachment_logs where message_id = $1 AND deleted IS TRUE"""
            attachment_results = await self.bot.pool.fetch(attachment_sql, message_id)
            for _index, result in enumerate(attachment_results):
                file = discord.File(
                    BytesIO(result["attachment"]),
                    filename=f'{message_id}_{_index}.{imghdr.what(None, result["attachment"])}',
                )
                files.append(file)
                embed = discord.Embed(
                    color=self.bot.embedcolor,
                    timestamp=results[index - 1]["created_at"],
                )
                embed.set_image(
                    url=f'attachment://{message_id}_{_index}.{imghdr.what(None, result["attachment"])}'
                )
                embeds.append(embed)

        await ctx.send(embeds=embeds[:10], files=files[:9])
        if len(embeds) >= 10:
            await ctx.send(embeds=embeds[-1:], files=files[-1:])

    @commands.command(name="invite", aliases=("join",))
    async def invite(self, ctx: commands.Context):
        """Sends an invite link to the bot"""
        bot = self.bot
        if bot.user is None:
            return

        permissions = discord.Permissions.none()
        permissions.read_messages = True
        permissions.send_messages = True
        permissions.read_message_history = True
        permissions.embed_links = True
        permissions.attach_files = True

        await ctx.send(
            f'{discord.utils.oauth_url(bot.user.id, permissions=permissions, scopes=("bot",))}'
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
            failed = False
            sql = """
            INSERT INTO download_logs(user_id, guild_id, video, time)
            VALUES ($1, $2, $3, $4)
            """
            await self.bot.pool.execute(
                sql, ctx.author.id, ctx.guild.id, video, discord.utils.utcnow()
            )
        except (ValueError, discord.Forbidden):
            await message.edit(content="Failed to download, try again later?")
            failed = True

        self.currently_downloading.remove(f"{default_name}.{default_format}")

        channel = self.bot.get_channel(998816503589781534)
        embed = discord.Embed(
            title="Video downloaded",
            timestamp=discord.utils.utcnow(),
            color=ctx.bot.embedcolor if not failed else discord.Color.red(),
        )
        embed.add_field(name="Author", value=f"{ctx.author}\n{ctx.author.mention}")
        embed.add_field(name="Video", value=video, inline=False)
        embed.set_footer(text=f"ID: {ctx.author.id} \nDownloaded at ")

        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=embed)

        if not failed:
            try:
                os.remove(f"files/videos/{default_name}.{default_format}")
            except (FileNotFoundError, PermissionError):
                pass

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
