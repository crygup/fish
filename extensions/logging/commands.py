from __future__ import annotations

import asyncio
import base64
import datetime
import random
from typing import TYPE_CHECKING, List, Tuple

import asyncpg
import discord
from discord.ext import commands

from core import Cog
from utils import (
    AvatarsPageSource,
    FieldPageSource,
    Pager,
    format_bytes,
    format_status,
    human_timedelta,
    to_image,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context, GuildContext


class Commands(Cog):
    async def avatars_func(self, ctx: Context, user: discord.User):
        sql = """
        SELECT * FROM avatars WHERE user_id = $1
        ORDER BY created_at DESC
        """

        async with ctx.typing():
            records: List[asyncpg.Record] = await self.bot.pool.fetch(sql, user.id)

            if not bool(records):
                raise commands.BadArgument(f"I have no avatars on record for {user}")

            entries: List[Tuple[str, datetime.datetime, int]] = [
                (
                    r["avatar"],
                    r["created_at"],
                    r["id"],
                )
                for r in records
            ]

            source = AvatarsPageSource(entries=entries)
            source.embed.color = (
                self.bot.embedcolor
                if user.color == discord.Color.default()
                else user.color
            )
            source.embed.title = f"Avatars for {user}"
            pager = Pager(source, ctx=ctx)
            await pager.start(ctx)

    @commands.hybrid_group(
        name="avatars", fallback="list", aliases=("pfps", "avis", "avs")
    )
    async def avatars(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows a user's previous avatars"""

        await self.avatars_func(ctx, user)

    async def avatars_grid(self, ctx: Context, user: discord.User):
        sql = """
        SELECT * FROM avatars WHERE user_id = $1
        ORDER BY created_at DESC LIMIT 100
        """

        async with ctx.typing():
            records: List[asyncpg.Record] = await self.bot.pool.fetch(sql, user.id)

            if not bool(records):
                raise commands.BadArgument(f"{user} has no avatars on record.")

            avatars = await asyncio.gather(
                *[to_image(ctx.session, row["avatar"], bytes=True) for row in records]
            )

            file = discord.File(
                await format_bytes(
                    ctx.guild.filesize_limit if ctx.guild else 8388608, avatars
                ),
                f"{user.id}_avatar_history.png",
            )

            if len(records) >= 100:
                first_avatar: datetime.datetime = await self.bot.pool.fetchval(
                    """SELECT created_at FROM avatars WHERE user_id = $1 ORDER BY created_at ASC""",
                    user.id,
                )
            else:
                first_avatar = records[-1]["created_at"]

            embed = discord.Embed(color=self.bot.embedcolor, timestamp=first_avatar)
            embed.set_image(url=f"attachment://{user.id}_avatar_history.png")
            embed.set_footer(text="First avatar saved")
            await ctx.send(
                f"Viewing avatars in a grid view for {user}", file=file, embed=embed
            )

    @commands.command(
        name="avatarhistory",
        aliases=("avyh", "avatar-history", "avatar_history", "pfph", "avh"),
        hidden=True,
    )
    async def avatar_history(
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        """Shows a user's previous avatars in a grid view"""
        await self.avatars_grid(ctx, user)

    @avatars.command(name="grid", fallback="list", aliases=("c", "collection"))
    async def avatars_grid_command(
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        """Shows a user's previous avatars in a grid view"""
        await self.avatars_grid(ctx, user)

    @commands.command(name="usernames")
    async def usernames(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows a user's previous usernames"""

        results = await self.bot.pool.fetch(
            "SELECT * FROM username_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if not bool(results):
            raise commands.BadArgument(f"I have no usernames on record for {user}")

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

    @commands.command(name="names", aliases=("display_names", "displaynames"))
    async def display_names(
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        """Shows a user's previous display names"""

        results = await self.bot.pool.fetch(
            "SELECT * FROM display_name_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if not bool(results):
            raise commands.BadArgument(f"I have no display names on records for {user}")

        entries = [
            (
                r["display_name"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")} | `ID: {r["id"]}`',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Display names for {user}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="nicknames", aliases=("nicks",))
    async def nicknames(
        self, ctx: Context, *, member: discord.Member = commands.Author
    ):
        """Shows a user's previous nicknames"""

        results = await self.bot.pool.fetch(
            "SELECT * FROM nickname_logs WHERE user_id = $1 AND guild_id = $2 ORDER BY created_at DESC",
            member.id,
            member.guild.id,
        )

        if not bool(results):
            raise commands.BadArgument(f"I have no nicknames on records for {member}")

        entries = [
            (
                r["nickname"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")} | `ID: {r["id"]}`',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Nicknames names for {member}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="discrims", aliases=("discriminators",))
    async def discrims(self, ctx: Context, *, member: discord.Member = commands.Author):
        """Shows a user's previous discrim_logs"""

        results = await self.bot.pool.fetch(
            "SELECT * FROM discrim_logs WHERE user_id = $1 ORDER BY created_at DESC",
            member.id,
        )

        if not bool(results):
            raise commands.BadArgument(
                f"I have no discriminators on records for {member}"
            )

        entries = [
            (
                r["discrim"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")} | `ID: {r["id"]}`',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Discriminators names for {member}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="servernames", aliases=("server_names", "snames"))
    @commands.guild_only()
    async def server_names(
        self, ctx: GuildContext, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Shows the server's previous names"""

        results = await self.bot.pool.fetch(
            "SELECT * FROM guild_name_logs WHERE guild_id = $1 ORDER BY created_at DESC",
            guild.id,
        )

        if not bool(results):
            raise commands.BadArgument(f"I have no server names on records for {guild}")

        entries = [
            (
                r["name"],
                f'{discord.utils.format_dt(r["created_at"], "R")}  |  {discord.utils.format_dt(r["created_at"], "d")} | `ID: {r["id"]}`',
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Names for {guild}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.hybrid_command(name="icons")
    async def icons(
        self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Shows a server's previous icons"""

        sql = """
        SELECT * FROM guild_icons WHERE guild_id = $1
        ORDER BY created_at DESC
        """

        async with ctx.typing():
            records: List[asyncpg.Record] = await self.bot.pool.fetch(sql, guild.id)

            if not bool(records):
                raise commands.BadArgument(f"I have no icons on record for {guild}")

            entries: List[Tuple[str, datetime.datetime, int]] = [
                (
                    r["icon"],
                    r["created_at"],
                    r["id"],
                )
                for r in records
            ]

            source = AvatarsPageSource(entries=entries)
            source.embed.color = self.bot.embedcolor
            source.embed.title = f"Icons for {guild}"
            pager = Pager(source, ctx=ctx)
            await pager.start(ctx)

    @commands.command(name="uptime")
    @commands.guild_only()
    async def uptime(
        self,
        ctx: GuildContext,
        *,
        member: discord.Member = commands.param(default=lambda ctx: ctx.bot.user),
    ):
        """Shows how long someone has been online"""
        if self.bot.user and self.bot.user.id == member.id:
            await ctx.send(
                f"Hi, I have been awake for {human_timedelta(self.bot.start_time, suffix=False)}"
            )
            return

        sql = """SELECT * FROM status_logs WHERE user_id = $1 AND guild_id = $2"""

        results = await self.bot.pool.fetchrow(sql, member.id, ctx.guild.id)

        if not bool(results):
            raise commands.BadArgument(f"I have no status records for {member}")

        await ctx.send(
            f"{member} has been {format_status(member)} for {human_timedelta(results['created_at'], suffix=False)}."
        )
