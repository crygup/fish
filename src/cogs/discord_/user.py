import datetime
import math
import time
from io import BytesIO
from typing import List, Optional, Union

import asyncpg
import discord
from bot import Bot, Context
from discord.ext import commands
from PIL import Image
from utils import FieldPageSource, Pager, human_timedelta, resize_to_limit, to_thread

from ._base import DiscordBase
from .views import AvatarView


class UserCommands(DiscordBase):
    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(name="avatar", aliases=("pfp", "avy", "av"))
    async def avatar(
        self, ctx: Context, user: Union[discord.Member, discord.User] = commands.Author
    ):
        """Gets the avatar of a user"""
        embed = discord.Embed(
            color=self.bot.embedcolor
            if user.color == discord.Color.default()
            else user.color
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)

        embed.set_image(url=user.display_avatar.url)
        sql = """SELECT created_at FROM avatar_logs WHERE user_id = $1 ORDER BY created_at DESC"""
        latest_avatar = await self.bot.pool.fetchval(sql, user.id)

        if latest_avatar:
            embed.timestamp = latest_avatar
            embed.set_footer(text="Avatar changed")

        await ctx.send(
            embed=embed,
            view=AvatarView(ctx, user, embed, user.display_avatar),
            check_ref=True,
        )

    @commands.command(name="banner")
    async def banner(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows a users banner"""

        user = await ctx.bot.fetch_user(user.id)
        if user.banner is None:
            raise TypeError("This user has no banner.")

        file = await user.banner.to_file(
            filename=f'banner.{"gif" if user.banner.is_animated() else "png"}'
        )
        embed = discord.Embed()
        embed.set_author(name=f"{str(user)}'s banner", icon_url=user.display_avatar.url)
        embed.set_image(
            url=f'attachment://banner.{"gif" if user.banner.is_animated() else "png"}'
        )
        await ctx.send(file=file, embed=embed)

    async def failed_to_find(
        self, ctx: Context, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        url = f"https://discordapp.com/channels/{guild_id}/{channel_id}/{message_id}"
        await ctx.send(
            f"I was unable to find and verify the message, here is a link, it might not work though. \b{url}"
        )

    @commands.command(name="first_message", aliases=("fmsg", "oldest"))
    async def first_message(
        self,
        ctx: Context,
        channel: Optional[
            Union[discord.TextChannel, discord.Thread, discord.VoiceChannel]
        ],
        *,
        user: Optional[discord.User],
    ):
        """Sends a url to the first message from a member in a channel.

        This is based on when fishie was added to the server."""

        if ctx.guild is None:
            return

        channel = channel or ctx.channel

        if user:
            sql = f"""SELECT * FROM message_logs WHERE author_id = $1 AND guild_id = $2 AND channel_id = $3 ORDER BY created_at ASC LIMIT 1"""
            args = [user.id, ctx.guild.id, channel.id]
        else:
            sql = f"""SELECT * FROM message_logs WHERE guild_id = $1 AND channel_id = $2 ORDER BY created_at ASC LIMIT 1"""
            args = [ctx.guild.id, channel.id]

        record = await self.bot.pool.fetchrow(sql, *args)
        if record is None:
            await ctx.send(f"It seems I have no records in this channel")
            return

        _channel = self.bot.get_channel(channel.id)
        if _channel is None or not isinstance(
            _channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)
        ):
            await self.failed_to_find(
                ctx, ctx.guild.id, channel.id, record["message_id"]
            )
            return

        try:
            _message = await _channel.fetch_message(record["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await self.failed_to_find(
                ctx, ctx.guild.id, channel.id, record["message_id"]
            )
            return

        await ctx.send(_message.jump_url)

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
        ctx: Context,
        guild: Optional[discord.Guild] = commands.CurrentGuild,
        *,
        user: discord.User = commands.Author,
    ):
        """Shows how many times a user joined a server

        Note: If they joined before I was added then I will not have any data for them."""

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
            f"{user} has joined {guild} {results:,} time{'s' if results > 1 else ''}."
        )

    @commands.command(name="uptime")
    async def uptime(self, ctx: Context, *, member: Optional[discord.Member]):
        """Shows how long a user has been online."""
        bot = self.bot
        me = bot.user

        if me is None or bot.uptime is None:
            return

        if member is None or member and member.id == me.id:
            await ctx.send(
                f"Hello, I have been awake for {human_timedelta(bot.uptime,suffix=False)}."
            )
            return

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
    async def usernames(self, ctx: Context, user: discord.User = commands.Author):

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
    async def discrims(self, ctx: Context, user: discord.User = commands.Author):
        """Shows all discriminators a user has had.

        This is the numbers after your username."""

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
        ctx: Context,
        *,
        user: discord.User = commands.Author,
    ):
        """Shows all nicknames a user has had in a guild."""
        if ctx.guild is None:
            return

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
        ctx: Context,
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
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        """Shows the avatar history of a user."""
        if ctx.guild is None:
            return

        async with ctx.typing():
            fetch_start = time.perf_counter()
            avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
                "SELECT * FROM avatar_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT 100",
                user.id,
            )
            fetch_end = time.perf_counter()

            if avatars == []:
                raise TypeError(f"{str(user)} has no avatar history on record.")

            gen_start = time.perf_counter()
            file = await self.do_avatar_command(ctx, user, avatars)
            gen_end = time.perf_counter()

        embed = discord.Embed(timestamp=avatars[-1]["created_at"])
        embed.set_footer(text="First avatar saved")
        embed.set_author(
            name=f"{user}'s avatar history", icon_url=user.display_avatar.url
        )
        embed.description = f"`Fetching  :` {round(fetch_end - fetch_start, 2)}s\n`Generating:` {round(gen_end - gen_start, 2)}s"
        embed.set_image(url=f"attachment://{user.id}_avatar_history.png")

        await ctx.send(embed=embed, file=file)

    @avatar_history.command(name="guild")
    async def avatar_history_guild(
        self, ctx: Context, *, member: discord.Member = commands.Author
    ):
        """Shows the guild avatar history of a user."""
        async with ctx.typing():
            fetch_start = time.perf_counter()
            avatars: List[asyncpg.Record] = await self.bot.pool.fetch(
                "SELECT avatar FROM guild_avatar_logs WHERE user_id = $1 AND guild_id = $2 ORDER BY created_at DESC LIMIT 100",
                member.id,
                ctx.guild.id,
            )
            fetch_end = time.perf_counter()

            if avatars == []:
                raise TypeError(f"{str(member)} has no avatar history on record.")

            gen_start = time.perf_counter()
            file = await self.do_avatar_command(ctx, member, avatars)
            gen_end = time.perf_counter()

        embed = discord.Embed(timestamp=avatars[-1]["created_at"])
        embed.set_footer(text="First avatar saved")
        embed.set_author(
            name=f"{member}'s avatar history in {ctx.guild}",
            icon_url=member.display_avatar.url,
        )
        embed.description = f"`Fetching  :` {round(fetch_end - fetch_start, 2)}s\n`Generating:` {round(gen_end - gen_start, 2)}s"
        embed.set_image(url=f"attachment://{member.id}_avatar_history.png")

        await ctx.send(content=f"Viewing guild avatar log for {member}", file=file)
