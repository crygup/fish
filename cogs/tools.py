import argparse
import datetime
import imghdr
import math
import os
import re
import secrets
import shlex
import textwrap
import time
from io import BytesIO
from typing import List, Optional

import asyncpg
import discord
from bot import Bot
from discord.ext import commands, tasks
from PIL import Image
from utils import (
    FieldPageSource,
    GuildContext,
    Pager,
    TenorUrlConverter,
    get_video,
    human_timedelta,
    regexes,
    resize_to_limit,
    run,
    to_thread,
)


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


class Tools(commands.Cog, name="tools"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.currently_downloading: list[str] = []

    async def cog_unload(self):
        self.delete_videos.cancel()

    async def cog_load(self) -> None:
        self.delete_videos.start()

    @commands.command(name="first_message", aliases=("fm", "oldest"))
    async def first_message(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        *,
        member: discord.Member = commands.Author,
    ):
        """Sends a url to the first message from a member in a channel.

        If the url seems to lead nowhere the message might've been deleted."""

        if ctx.guild is None:
            return

        channel = channel or ctx.channel  # type: ignore

        if channel is None:
            return

        await self.bot.get_cog("message_event")._delete_videos()  # type: ignore

        record = await self.bot.pool.fetchrow(
            "SELECT * FROM message_logs WHERE author_id = $1 AND guild_id = $2 AND channel_id = $3 ORDER BY created_at ASC LIMIT 1",
            member.id,
            ctx.guild.id,
            channel.id,
        )
        if record is None:
            await ctx.send(
                f"It seems I have no records for {str(member)} in this channel"
            )
            return

        url = f'https://discordapp.com/channels/{record["guild_id"]}/{record["channel_id"]}/{record["message_id"]}'
        await ctx.send(url)

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

        await self.bot.get_cog("message_event")._delete_videos()  # type: ignore

        if member:
            sql = """
            SELECT * FROM snipe_logs where channel_id = $1 AND author_id = $2 ORDER BY created_at DESC
            """
            results = await self.bot.pool.fetch(sql, channel.id, member.id)
        else:
            sql = """
            SELECT * FROM snipe_logs where channel_id = $1 ORDER BY created_at DESC
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

        attachment_sql = """SELECT * FROM snipe_attachment_logs where message_id = $1"""
        attachment_results = await self.bot.pool.fetch(attachment_sql, message_id)
        for _index, result in enumerate(attachment_results):
            file = discord.File(
                BytesIO(result["attachment"]),
                filename=f'{message_id}_{_index}.{imghdr.what(None, result["attachment"])}',
            )
            files.append(file)
            embed = discord.Embed(
                color=self.bot.embedcolor, timestamp=results[index - 1]["created_at"]
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

    async def _index_member(
        self, guild: discord.Guild, user: discord.Member | discord.User
    ) -> bool:
        member = guild.get_member(user.id)

        if member is None:
            return False

        joined = member.joined_at

        if joined is None:
            return False

        await self.bot.pool.execute(
            "INSERT INTO member_join_logs (member_id, guild_id, time) VALUES ($1, $2, $3)",
            user.id,
            guild.id,
            joined,
        )

        return True

    @commands.group(name="joins", invoke_without_command=True)
    async def joins(
        self,
        ctx: commands.Context,
        guild: Optional[discord.Guild] = None,
        *,
        user: discord.User = commands.Author,
    ):
        """Shows how many times a user joined a server

        Note: If they joined before I was added then I will not have any data for them."""
        await self.bot.get_cog("guild_events")._delete_videos()  # type: ignore

        guild = guild or ctx.guild

        if guild is None:
            return

        results: Optional[int] = await self.bot.pool.fetchval(
            "SELECT COUNT(member_id) FROM member_join_logs WHERE member_id = $1 AND guild_id = $2",
            user.id,
            guild.id,
        )

        if results == 0 or results is None:
            results = await self._index_member(guild, user)
            if results:
                results = 1

            else:
                await ctx.send(f"I have no join records for {user!s} in {guild!s}")
                return

        await ctx.send(
            f"{user!s} has joined {guild!s} {results:,} time{'s' if results > 1 else ''}."
        )

    @joins.group(name="index", invoke_without_command=True)
    async def joins_index(self, ctx: commands.Context):
        """Adds your join date to the database."""
        if ctx.guild is None:
            return

        await self.bot.get_cog("guild_events")._delete_videos()  # type: ignore

        records = await self.bot.pool.fetch(
            "SELECT * FROM member_join_logs WHERE member_id = $1 AND guild_id = $2",
            ctx.author.id,
            ctx.guild.id,
        )

        if records != []:
            await ctx.send("You are already indexed in this server.")
            return

        member = ctx.guild.get_member(ctx.author.id)

        if member is None:
            return

        joined = member.joined_at

        if joined is None:
            return

        await self._index_member(ctx.guild, ctx.author)

        await ctx.send(
            f"Added you. You joined on {discord.utils.format_dt(joined, 'D')}."
        )

    @commands.command(name="uptime")
    async def uptime(self, ctx: commands.Context, *, member: Optional[discord.Member]):
        bot = self.bot
        me = bot.user

        if me is None or bot.uptime is None:
            return

        if member is None or member and member.id == me.id:
            await ctx.send(
                f"Hello, I have been awake for {human_timedelta(bot.uptime,suffix=False)}."
            )
            return

        await self.bot.get_cog("user_events")._delete_videos()  # type: ignore

        results: Optional[datetime.datetime] = await bot.pool.fetchval(
            "SELECT time FROM uptime_logs WHERE user_id = $1", member.id
        )

        if results is None:
            await ctx.send(
                f'{member} has been {"on " if member.status is discord.Status.dnd else ""}{member.raw_status} as long as I can tell.'
            )
            return

        await ctx.send(
            f'{member} has been {"on " if member.status is discord.Status.dnd else ""}{member.raw_status} for {human_timedelta(results,suffix=False)}.'
        )

    @commands.command(name="usernames", aliases=("names",))
    async def usernames(
        self, ctx: commands.Context, user: discord.User = commands.Author
    ):

        await self.bot.get_cog("user_events")._delete_videos()  # type: ignore

        results = await self.bot.pool.fetch(
            "SELECT * FROM username_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if results == []:
            await ctx.send(f"I have no username records for {user}")
            return

        entries = [
            (
                r["username"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Usernames for {user}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="discrims", aliases=("discriminators",))
    async def discrims(
        self, ctx: commands.Context, user: discord.User = commands.Author
    ):
        await self.bot.get_cog("user_events")._delete_videos()  # type: ignore

        results = await self.bot.pool.fetch(
            "SELECT * FROM discrim_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if results == []:
            await ctx.send(f"I have no discriminator records for {user}")
            return

        entries = [
            (
                f'#{r["discrim"]}',
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Discriminators for {user}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="nicknames", aliases=("nicks",))
    async def nicknames(
        self,
        ctx: commands.Context,
        *,
        user: discord.User = commands.Author,
    ):
        if ctx.guild is None:
            return

        await self.bot.get_cog("member_events")._delete_videos()  # type: ignore

        results = await self.bot.pool.fetch(
            "SELECT * FROM nickname_logs WHERE user_id = $1 AND guild_id = $2 ORDER BY created_at DESC",
            user.id,
            ctx.guild.id,
        )

        if results == []:
            await ctx.send(f"I have no nickname records for {user} in {ctx.guild}")
            return

        entries = [
            (
                r["nickname"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")}',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Nicknames for {user} in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    # https://github.com/CuteFwan/Koishi/blob/master/cogs/avatar.py#L82-L102
    @to_thread
    def _make_avatars(self, filesize_limit: int, avatars: List[bytes]) -> BytesIO:
        xbound = math.ceil(math.sqrt(len(avatars)))
        ybound = math.ceil(len(avatars) / xbound)
        size = int(2520 / xbound)

        with Image.new(
            "RGBA", size=(xbound * size, ybound * size), color=(0, 0, 0, 0)
        ) as base:
            x, y = 0, 0
            for avy in avatars:
                if avy:
                    im = Image.open(BytesIO(avy)).resize(
                        (size, size), resample=Image.BICUBIC
                    )
                    base.paste(im, box=(x * size, y * size))
                if x < xbound - 1:
                    x += 1
                else:
                    x = 0
                    y += 1
            buffer = BytesIO()
            base.save(buffer, "png")
            buffer.seek(0)
            buffer = resize_to_limit(buffer, filesize_limit)
            return buffer

    async def do_avatar_command(
        self,
        ctx: commands.Context,
        user: discord.User | discord.Member,
        avatars: List[asyncpg.Record],
    ) -> discord.File:

        if ctx.guild is None:
            raise commands.GuildNotFound("Guild not found")

        fp = await self._make_avatars(
            ctx.guild.filesize_limit, [x["avatar"] for x in avatars]
        )
        file = discord.File(
            fp,
            f"{user.id}_avatar_history.png",
        )

        return file

    @commands.group(
        name="avatarhistory", aliases=("avyh",), invoke_without_command=True
    )
    async def avatar_history(
        self, ctx: commands.Context, *, user: discord.User = commands.Author
    ):
        """Shows the avatar history of a user."""
        if ctx.guild is None:
            return

        await self.bot.get_cog("user_events")._delete_videos()  # type: ignore

        check = await self.bot.pool.fetchrow(
            "SELECT avatar FROM avatar_logs WHERE user_id = $1", user.id
        )

        if check is None:
            raise TypeError(f"{str(user)} has no avatar history on record.")

        async with ctx.typing():
            avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
                "SELECT avatar FROM avatar_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT 100",
                user.id,
            )
            file = await self.do_avatar_command(ctx, user, avatars)

        await ctx.send(content=f"Viewing avatar log for {str(user)}", file=file)

    @avatar_history.command(name="guild")
    async def avatar_history_guild(
        self, ctx: commands.Context, *, member: discord.Member = commands.Author
    ):
        """Shows the guild avatar history of a user."""
        if ctx.guild is None:
            return

        await self.bot.get_cog("member_events")._delete_videos()  # type: ignore

        check = await self.bot.pool.fetchrow(
            "SELECT avatar FROM guild_avatar_logs WHERE user_id = $1 AND guild_id = $2",
            member.id,
            ctx.guild.id,
        )

        if check is None:
            raise TypeError(f"{str(member)} has no avatar guild history on record.")

        async with ctx.typing():
            avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
                "SELECT avatar FROM guild_avatar_logs WHERE user_id = $1 AND guild_id = $2 ORDER BY created_at DESC LIMIT 100",
                member.id,
                ctx.guild.id,
            )
            file = await self.do_avatar_command(ctx, member, avatars)

        await ctx.send(content=f"Viewing guild avatar log for {member}", file=file)

    @commands.command(name="tenor")
    async def tenor(self, ctx: commands.Context, url: str):
        """Gets the actual gif URL from a tenor link"""

        real_url = await TenorUrlConverter().convert(ctx, url)
        await ctx.send(f"Here is the real url: {real_url}")

    @commands.command(name="download", hidden=True)
    async def download(self, ctx: GuildContext, url: str, *, flags: Optional[str]):
        """Downloads a video from certain sites.

        Accepted sites are, Youtube, TikTok, Twitter, Twitch, and reddit.

        Flags:
            -format: The format of the video.
            -name: The name of the file."""

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

        basic_method = f'yt-dlp {video} -P "files/videos" -o "{default_name}.%(ext)s" '

        if audio_only:
            basic_method += f"-i --extract-audio --audio-format {default_format}"
        else:
            # tiktok uses h264 encoding so we have to use this
            # in the future i will add more checks to if this is reaccuring issue with other platforms
            # but for now ternary is fine
            basic_method += (
                "-S vcodec:h264"
                if re.fullmatch(regexes["VMtiktok"]["regex"], video)
                or re.fullmatch(regexes["WEBtiktok"]["regex"], video)
                else f'--format "bestvideo+bestaudio[ext={default_format}]/best"'
            )

        message = await ctx.send("Downloading video")

        self.currently_downloading.append(f"{default_name}.{default_format}")
        start = time.perf_counter()
        await run(basic_method)
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
