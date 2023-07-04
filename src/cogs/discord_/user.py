from __future__ import annotations

import asyncio
import datetime
import time
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

import asyncpg
import discord
from discord.ext import commands

from utils import (
    AvatarsPageSource,
    AvatarView,
    FieldPageSource,
    Pager,
    format_bytes,
    human_timedelta,
    to_bytes,
    format_status,
    BlankException,
    BaseCog,
)

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


class UserCommands(BaseCog):
    @commands.command(name="avatar", aliases=("pfp", "avy", "av"))
    async def avatar(
        self,
        ctx: Context,
        *,
        user: Optional[Union[discord.Member, discord.User]] = commands.Author,
    ):
        """Gets the avatar of a user"""
        user = user or ctx.author

        embed = discord.Embed(
            color=self.bot.embedcolor
            if user.color == discord.Color.default()
            else user.color
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)

        embed.set_image(url=user.display_avatar.url)

        sql = """SELECT created_at FROM avatars WHERE user_id = $1 ORDER BY created_at DESC"""
        latest_avatar = await self.bot.pool.fetchval(sql, user.id)

        if latest_avatar:
            embed.timestamp = latest_avatar
            embed.set_footer(text="Avatar changed")

        await ctx.send(
            embed=embed,
            view=AvatarView(ctx, user, embed, user.display_avatar),
            check_ref=True,
        )

    @commands.command(name="avatars", aliases=("pfps", "avys", "avs"))
    async def avatars(
        self, ctx: Context, user: Union[discord.Member, discord.User] = commands.Author
    ):
        """Shows all of a users avatars"""
        sql = """SELECT * FROM avatars WHERE user_id = $1 ORDER BY created_at DESC"""
        results = await self.bot.pool.fetch(sql, user.id)

        if results == []:
            raise ValueError("User has no avatar history saved.")

        entries: List[Tuple[str, datetime.datetime, int]] = [
            (
                r["avatar"],
                r["created_at"],
                r["id"],
            )
            for r in results
        ]

        source = AvatarsPageSource(entries=entries)
        source.embed.color = (
            self.bot.embedcolor if user.color == discord.Color.default() else user.color
        )
        source.embed.title = f"Avatars for {user}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

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

    async def _index_member(self, guild: discord.Guild, member: discord.Member) -> bool:
        sql = """
        INSERT INTO member_join_logs (member_id, guild_id, time)
        SELECT $1, $2, $3
        WHERE NOT EXISTS (
            SELECT 1
            FROM member_join_logs
            WHERE member_id = $1 AND guild_id = $2 AND time = $3
        ); 
        """
        await self.bot.pool.execute(
            sql,
            member.id,
            guild.id,
            member.joined_at,
        )

        return True

    @commands.group(name="joins", invoke_without_command=True)
    async def joins(
        self,
        ctx: Context,
        *,
        user: Union[discord.Member, discord.User] = commands.Author,
    ):
        """Shows how many times a user joined a server

        Note: If they joined before I was added then I will not have any data for them.
        """

        guild = ctx.guild

        results: Optional[int] = await self.bot.pool.fetchval(
            "SELECT COUNT(member_id) FROM member_join_logs WHERE member_id = $1 AND guild_id = $2",
            user.id,
            guild.id,
        )

        if not results:
            if isinstance(user, discord.Member):
                results = await self._index_member(guild, user)

            if results:
                results = 1

            else:
                return await ctx.send(f"I have no join records for {user} in {guild}")

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
            return await ctx.send(
                f"Hello, I have been awake for {human_timedelta(bot.uptime, suffix=False)}."
            )

        if "uptime" in await self.bot.redis.smembers(f"opted_out:{member.id}"):
            raise BlankException(f"Sorry, {member} has opted out from uptime logging.")

        results: Optional[datetime.datetime] = await bot.pool.fetchval(
            "SELECT time FROM uptime_logs WHERE user_id = $1", member.id
        )

        message = (
            f"{member} has been {format_status(member)} for {human_timedelta(results, suffix=False)}."
            if results
            else f"{member} has been {format_status(member)} as long as I can tell."
        )

        await ctx.send(message)

    @commands.command(name="usernames", aliases=("names",))
    async def usernames(self, ctx: Context, user: discord.User = commands.Author):
        results = await self.bot.pool.fetch(
            "SELECT * FROM username_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if results == []:
            await ctx.send(f"I have no username records for {user}.")
            return

        entries = [
            (
                r["username"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")} | `ID: {r["id"]}`',
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
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")} | `ID: {r["id"]}`',
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
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")} | `ID: {r["id"]}`',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.title = f"Nicknames for {user} in {ctx.guild}"
        source.embed.color = self.bot.embedcolor
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.group(
        name="avatarhistory",
        aliases=("avyh", "pfph", "avh"),
        invoke_without_command=True,
    )
    async def avatar_history(
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        """Shows the avatar history of a user.

        This will only show the first 100, to view them all and in HD run the command `avatars`
        """

        async with ctx.typing():
            sql = """
            SELECT * FROM avatars WHERE user_id = $1
            ORDER BY created_at DESC LIMIT 100
            """

            records: List[asyncpg.Record] = await self.bot.pool.fetch(
                sql,
                user.id,
            )

            if records == []:
                await ctx.send(f"{user} has no avatar history on record.")
                return

            avatars = await asyncio.gather(
                *[to_bytes(ctx.session, row["avatar"]) for row in records]
            )

            fp = await format_bytes(ctx.guild.filesize_limit, avatars)
            file = discord.File(
                fp,
                f"{user.id}_avatar_history.png",
            )

        if len(records) >= 100:
            first_avatar: datetime.datetime = await self.bot.pool.fetchval(
                """SELECT created_at FROM avatars WHERE user_id = $1 ORDER BY created_at ASC""",
                user.id,
            )
        else:
            first_avatar = records[-1]["created_at"]

        embed = discord.Embed(timestamp=first_avatar)
        embed.set_footer(text="First avatar saved")
        embed.set_author(
            name=f"{user}'s avatar history", icon_url=user.display_avatar.url
        )
        embed.set_image(url=f"attachment://{user.id}_avatar_history.png")

        await ctx.send(embed=embed, file=file)

    @avatar_history.command(name="server", aliases=("guild",))
    async def avatar_history_guild(
        self,
        ctx: Context,
        guild: Optional[discord.Guild] = None,
        *,
        member: discord.Member = commands.Author,
    ):
        """Shows the server avatar history of a user."""
        guild = guild or ctx.guild

        async with ctx.typing():
            sql = """
            SELECT * FROM guild_avatars WHERE member_id = $1 AND guild_id = $2
            ORDER BY created_at DESC LIMIT 100
            """

            fetch_start = time.perf_counter()
            records: List[asyncpg.Record] = await self.bot.pool.fetch(
                sql, member.id, guild.id
            )
            fetch_end = time.perf_counter()

            if records == []:
                raise ValueError(f"{member} has no server avatar history on record.")

            avatars = await asyncio.gather(
                *[to_bytes(ctx.session, row["avatar"]) for row in records]
            )

            gen_start = time.perf_counter()
            fp = await format_bytes(guild.filesize_limit, avatars)
            file = discord.File(
                fp,
                f"{member.id}_avatar_history.png",
            )
            gen_end = time.perf_counter()

        if len(records) == 100:
            sql = """SELECT created_at FROM guild_avatars WHERE member_id = $1 AND guild_id = $1 ORDER BY created_at ASC"""
            first_avatar: datetime.datetime = await self.bot.pool.fetchval(
                sql, member.id, guild.id
            )
        else:
            first_avatar = records[-1]["created_at"]

        embed = discord.Embed(timestamp=first_avatar)
        embed.set_footer(text="First avatar saved")
        embed.set_author(
            name=f"{member}'s guild avatar history", icon_url=member.display_avatar.url
        )
        embed.description = f"`Fetching  :` {round(fetch_end - fetch_start, 2)}s\n`Generating:` {round(gen_end - gen_start, 2)}s"
        embed.set_image(url=f"attachment://{member.id}_avatar_history.png")

        await ctx.send(embed=embed, file=file)
