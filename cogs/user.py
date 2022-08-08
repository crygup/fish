import datetime
import math
from io import BytesIO
from typing import List, Optional

import asyncpg
import discord
from bot import Bot, Context
from discord.ext import commands
from PIL import Image
from utils import (
    FieldPageSource,
    GuildContext,
    Pager,
    human_timedelta,
    resize_to_limit,
    to_thread,
)


async def setup(bot: Bot):
    await bot.add_cog(User(bot))


class User(commands.Cog, name="user"):
    """User related commands"""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.aliases = ["member", "members", "users"]

    @commands.command(name="first_message", aliases=("fmsg", "oldest"))
    async def first_message(
        self,
        ctx: Context,
        channel: Optional[discord.TextChannel],
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
        guild: Optional[discord.Guild] = None,
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
            f"{user!s} has joined {guild!s} {results:,} time{'s' if results > 1 else ''}."
        )

    @joins.group(name="index", invoke_without_command=True)
    async def joins_index(self, ctx: Context):
        """Adds your join date to the database."""
        if ctx.guild is None:
            return

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
        self, ctx: Context, *, member: discord.Member = commands.Author
    ):
        """Shows the guild avatar history of a user."""
        if ctx.guild is None:
            return

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
