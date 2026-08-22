from __future__ import annotations

import asyncio
import datetime
import re
import time
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, cast

import asyncpg
import discord
from discord import app_commands, utils
from discord.ext import commands, menus
from discord.http import Route

from core import Cog
from utils import (
    AvatarsPageSource,
    FieldPageSource,
    Pager,
    format_bytes,
    get_or_fetch_user,
    human_timedelta,
    plural,
    to_image,
)
from utils.activities import (
    ActivityLike,
    activity_details,
    activity_identity,
    activity_image_url,
    activity_matches,
    activity_name,
    activity_type_name,
)

from .status_calendar import (
    StatusInterval,
    hourly_statuses,
    render_status_calendar,
)

AVATAR_GRID_RE = re.compile(
    r"^(?P<width>\d{1,2})\s*x\s*(?P<height>\d{1,2})$", re.IGNORECASE
)

if TYPE_CHECKING:
    from extensions.context import Context, GuildContext


class ActivityPageSource(menus.ListPageSource):
    def __init__(
        self,
        entries: list[tuple[ActivityLike, list[discord.Member]]],
        *,
        color: discord.Color,
    ) -> None:
        super().__init__(entries, per_page=1)
        self.color = color

    async def format_page(
        self,
        menu: Pager,
        entry: tuple[ActivityLike, list[discord.Member]],
    ) -> discord.Embed:
        activity, members = entry
        embed = discord.Embed(
            title=f"{activity_type_name(activity)} • {activity_name(activity)}",
            color=self.color,
        )
        for label, value in activity_details(activity):
            embed.add_field(name=label, value=value[:1024], inline=True)
        embed.add_field(
            name=f"Members sharing this activity ({len(members)})",
            value="\n".join(member.mention for member in members) or "None",
            inline=False,
        )
        image_url = activity_image_url(activity)
        if image_url:
            embed.set_thumbnail(url=image_url)
        maximum = self.get_max_pages()
        if maximum and maximum > 1:
            embed.set_footer(text=f"Page {menu.current_page + 1}/{maximum}")
        return embed


class Commands(Cog):
    def ensure_history_visible(
        self,
        ctx: Context,
        user: discord.User | discord.Member,
    ) -> None:
        if user.bot:
            self.bot.db_cache.remember_user(user.id, is_bot=True)
            return
        if user.id != ctx.author.id and not self.bot.db_cache.user_history_is_public(
            user.id
        ):
            raise commands.BadArgument(
                f"{user} has their saved history private. Manage this in `fish settings`"
            )

    async def refresh_urls(self, attachment_urls: List[str]) -> List[str]:
        json = {"attachment_urls": attachment_urls}

        req = await self.bot.http.request(
            Route("POST", "/attachments/refresh-urls"),
            json=json,
        )

        refreshed_urls = req.get("refreshed_urls", [])
        return [url["refreshed"] for url in refreshed_urls]

    @staticmethod
    async def _activity_member(
        ctx: GuildContext,
        query: str,
    ) -> discord.Member | None:
        raw = query.strip()
        mention = re.fullmatch(r"<@!?(\d+)>", raw)
        user_id = (
            int(mention.group(1)) if mention else int(raw) if raw.isdigit() else None
        )
        if user_id is not None:
            member = ctx.guild.get_member(user_id)
            if member is not None:
                return member
            try:
                return await ctx.guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        lowered = raw.casefold()
        return next(
            (
                member
                for member in ctx.guild.members
                if lowered
                in {
                    member.name.casefold(),
                    member.display_name.casefold(),
                    (member.global_name or "").casefold(),
                }
            ),
            None,
        )

    @commands.hybrid_command(name="activity", aliases=("playing",))
    @commands.guild_only()
    @app_commands.describe(query="Activity name or server member to look up")
    async def activity(
        self,
        ctx: GuildContext,
        *,
        query: str | None = None,
    ) -> None:
        """Shows activity details and members sharing that activity."""
        target = (
            ctx.author if query is None else await self._activity_member(ctx, query)
        )
        grouped: dict[
            tuple[int, str, int | None],
            tuple[ActivityLike, list[discord.Member]],
        ] = {}

        if target is not None:
            if not target.activities:
                raise commands.BadArgument(f"{target} is not showing an activity.")
            for activity in target.activities:
                key = activity_identity(activity)
                members = [
                    member
                    for member in ctx.guild.members
                    if any(
                        activity_identity(candidate) == key
                        for candidate in member.activities
                    )
                ]
                grouped[key] = (activity, members)
        else:
            assert query is not None
            candidates: dict[
                tuple[int, str, int | None],
                tuple[ActivityLike, list[discord.Member]],
            ] = {}
            exact: set[tuple[int, str, int | None]] = set()
            for member in ctx.guild.members:
                for activity in member.activities:
                    if not activity_matches(activity, query):
                        continue
                    key = activity_identity(activity)
                    if key not in candidates:
                        candidates[key] = (activity, [])
                    if member not in candidates[key][1]:
                        candidates[key][1].append(member)
                    if activity_name(activity).casefold() == query.casefold():
                        exact.add(key)
            grouped = {key: candidates[key] for key in exact} if exact else candidates

        if not grouped:
            raise commands.BadArgument("I could not find anyone using that activity.")

        pages: list[tuple[ActivityLike, list[discord.Member]]] = []
        for activity, members in grouped.values():
            for chunk in discord.utils.as_chunks(members, max_size=15):
                pages.append((activity, list(chunk)))
        source = ActivityPageSource(pages, color=discord.Color(self.bot.embedcolor))
        await Pager(source, ctx=ctx).start(ctx)

    async def avatars_func(
        self, ctx: Context, user: discord.User, guild_id: Optional[int] = None
    ):
        self.ensure_history_visible(ctx, user)
        sql = (
            """SELECT * FROM guild_avatars WHERE member_id = $1 AND guild_id = $2 ORDER BY created_at DESC"""
            if guild_id
            else """SELECT * FROM avatars WHERE user_id = $1 ORDER BY created_at DESC"""
        )

        if guild_id:
            args = (sql, user.id, guild_id)
        else:
            args = (sql, user.id)

        async with ctx.typing():
            records: List[asyncpg.Record] = await self.bot.pool.fetch(*args)  # type: ignore # i think this is a typing bug, not stubbed properly

            if not bool(records):
                scope = "server avatars" if guild_id else "avatars"
                raise commands.BadArgument(f"{user} has no {scope} on record.")

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
            source.embed.title = (
                f"{['Avatars', 'Guild avatars'][bool(guild_id)]} for {user}"
            )
            source.embed.description = f"-# View all avatars [here](https://crygup.com/discord?tab=user&subtab=avatars&q={user.id})"
            pager = Pager(source, ctx=ctx)
            await pager.start(ctx)

    async def avatars_grid(
        self,
        ctx: Context,
        user: discord.User,
        guild_id: Optional[int] = None,
        grid_size: tuple[int, int] | None = None,
    ):
        self.ensure_history_visible(ctx, user)
        xbound, ybound = grid_size or (0, 0)
        record_limit = xbound * ybound if grid_size else 100
        sql = (
            """SELECT * FROM guild_avatars WHERE member_id = $1 AND guild_id = $2 ORDER BY created_at DESC LIMIT $3"""
            if guild_id
            else """SELECT * FROM avatars WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2"""
        )

        if guild_id:
            args = (sql, user.id, guild_id, record_limit)
            table = "guild_avatars"
            user_id = "member_id"
        else:
            args = (sql, user.id, record_limit)
            table = "avatars"
            user_id = "user_id"

        async with ctx.typing():
            records: List[asyncpg.Record] = await self.bot.pool.fetch(*args)  # type: ignore # same as above

            if not bool(records):
                scope = "server avatars" if guild_id else "avatars"
                raise commands.BadArgument(f"{user} has no {scope} on record.")

            urls = [record["avatar"] for record in records]
            refreshed = [
                url
                for chunk in utils.as_chunks(urls, max_size=50)
                for url in await self.refresh_urls(chunk)
            ]

            avatars = await asyncio.gather(
                *[to_image(ctx.session, url, bytes=True) for url in refreshed]
            )

            file = discord.File(
                await format_bytes(
                    ctx.guild.filesize_limit if ctx.guild else 8388608,
                    avatars,  # type: ignore
                    xbound=xbound,
                    ybound=ybound,
                ),
                f"{user.id}_avatar_history.png",
            )

            if len(records) >= record_limit:
                first_avatar: datetime.datetime = await self.bot.pool.fetchval(
                    f"""SELECT created_at FROM {table} WHERE {user_id} = $1 ORDER BY created_at ASC""",
                    user.id,
                )
            else:
                first_avatar = records[-1]["created_at"]

            embed = discord.Embed(color=self.bot.embedcolor, timestamp=first_avatar)

            embed.set_image(url=f"attachment://{user.id}_avatar_history.png")
            embed.set_author(
                name=f"{user.display_name}'s avatar in a grid view.",
                icon_url=user.display_avatar.url,
            )
            embed.set_footer(text="First avatar saved")
            embed.description = f"-# View all avatars [here](https://crygup.com/discord?tab=user&subtab=avatars&q={user.id})"
        await ctx.send(file=file, embed=embed)

    @staticmethod
    async def _avatar_history_arguments(
        ctx: Context,
        arguments: tuple[str, ...],
    ) -> tuple[discord.User, tuple[int, int] | None, bool]:
        """Parse an optional WxH grid, user, and server scope.

        ``server``/``guild`` is accepted here as well as by the explicit
        ``avatarhistory server`` subcommand.  This keeps the text command
        order-independent and prevents ``server`` from being passed to the
        user converter as a username.
        """
        grid_size: tuple[int, int] | None = None
        user_arguments: list[str] = []
        server = False

        for argument in arguments:
            if argument.strip().casefold() in {"server", "guild"}:
                server = True
                continue
            match = AVATAR_GRID_RE.fullmatch(argument.strip())
            if match:
                if grid_size is not None:
                    raise commands.BadArgument("Only one avatar grid size is allowed.")
                width = int(match.group("width"))
                height = int(match.group("height"))
                if not 1 <= width <= 10 or not 1 <= height <= 10:
                    raise commands.BadArgument(
                        "Avatar grid sizes must be between 1x1 and 10x10."
                    )
                grid_size = (width, height)
            else:
                user_arguments.append(argument)

        if not user_arguments:
            return ctx.author, grid_size, server  # type: ignore[return-value]

        raw_user = " ".join(user_arguments).strip()
        try:
            user = await commands.UserConverter().convert(ctx, raw_user)
        except commands.UserNotFound as error:
            raise commands.BadArgument(
                f"Could not find a user matching {raw_user}."
            ) from error
        return user, grid_size, server

    @commands.group(name="avatars", aliases=("pfps", "avis", "avs"))
    async def avatars(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows a user's previous avatars"""

        await self.avatars_func(ctx, user)

    @avatars.command(name="server", aliases=("guild", "s"))
    @commands.guild_only()
    async def server_avatars(
        self, ctx: GuildContext, *, user: discord.User = commands.Author
    ):
        """Shows a member's previous server avatars"""

        await self.avatars_func(ctx, user, ctx.guild.id)

    @commands.group(
        name="avatarhistory",
        aliases=("avyh", "avatar-history", "avatar_history", "pfph", "avh"),
        extras={"usage": "[grid_size] [user]"},
        invoke_without_command=True,
    )
    async def avatar_history(self, ctx: Context, *arguments: str):
        """Shows a user's previous avatars in a grid view."""
        user, grid_size, use_server = await self._avatar_history_arguments(
            ctx, arguments
        )

        guild_id = ctx.guild.id if use_server and ctx.guild else None
        if use_server and ctx.guild is None:
            raise commands.NoPrivateMessage(
                "Server avatar history can only be viewed in a server."
            )
        await self.avatars_grid(ctx, user, guild_id, grid_size=grid_size)

    @avatar_history.command(
        name="server",
        aliases=("guild", "s"),
        extras={"usage": "[grid_size] [user]"},
    )
    @commands.guild_only()
    async def server_avatar_history(self, ctx: Context, *arguments: str):
        """Shows a user's previous server avatars in a grid view."""
        assert ctx.guild

        user, grid_size, _ = await self._avatar_history_arguments(ctx, arguments)
        await self.avatars_grid(ctx, user, ctx.guild.id, grid_size=grid_size)

    async def _usernames(self, ctx: Context, user: discord.User) -> None:
        self.ensure_history_visible(ctx, user)
        results = await self.bot.pool.fetch(
            "SELECT * FROM username_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if not bool(results):
            raise commands.BadArgument(f"I have no usernames on record for {user}")

        entries = [
            (
                r["username"],
                f"{discord.utils.format_dt(r['created_at'], 'R')}  |  {discord.utils.format_dt(r['created_at'], 'd')} | `ID: {r['id']}`",
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Usernames for {user}"
        source.embed.description = f"-# View all usernames [here](https://crygup.com/discord?tab=user&subtab=usernames&q={user.id})"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="usernames")
    async def usernames(self, ctx: Context, *, user: discord.User = commands.Author):
        """Shows a user's previous usernames"""
        await self._usernames(ctx, user)

    @commands.command(name="servertags", aliases=("stags", "servertag"))
    async def server_tags(
        self, ctx: Context, *, user: discord.User = commands.Author
    ) -> None:
        """Shows a user's previous primary server tags."""
        await self._server_tags(ctx, user)

    async def _server_tags(self, ctx: Context, user: discord.User) -> None:
        self.ensure_history_visible(ctx, user)
        results = await self.bot.pool.fetch(
            "SELECT * FROM stag_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if not results:
            raise commands.BadArgument(f"I have no server tags on record for {user}")

        entries: List[Tuple[str, str]] = []
        for record in results:
            tag = discord.utils.escape_markdown(record["tag"] or "Removed tag")
            details = [
                f"{discord.utils.format_dt(record['created_at'], 'R')}  |  "
                f"{discord.utils.format_dt(record['created_at'], 'd')} | "
                f"`ID: {record['id']}`"
            ]
            entries.append((tag, "\n".join(details)))

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Server tags for {user}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    async def _display_names(self, ctx: Context, user: discord.User) -> None:
        self.ensure_history_visible(ctx, user)
        results = await self.bot.pool.fetch(
            "SELECT * FROM display_name_logs WHERE user_id = $1 ORDER BY created_at DESC",
            user.id,
        )

        if not bool(results):
            raise commands.BadArgument(f"I have no display names on records for {user}")

        entries = [
            (
                r["display_name"],
                f"{discord.utils.format_dt(r['created_at'], 'R')}  |  {discord.utils.format_dt(r['created_at'], 'd')} | `ID: {r['id']}`",
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Display names for {user}"
        source.embed.description = f"-# View all display names [here](https://crygup.com/discord?tab=user&subtab=display-names&q={user.id})"
        pager = Pager(source, ctx=ctx)

        await pager.start(ctx)

    @commands.command(name="names", aliases=("display_names", "displaynames"))
    async def display_names(
        self, ctx: Context, *, user: discord.User = commands.Author
    ):
        """Shows a user's previous display names"""
        await self._display_names(ctx, user)

    @commands.command(name="nicknames", aliases=("nicks",))
    @commands.guild_only()
    async def nicknames(
        self, ctx: Context, *, member: discord.Member = commands.Author
    ):
        """Shows a user's previous nicknames"""
        await self._nicknames(ctx, member)

    async def _nicknames(self, ctx: Context, member: discord.Member) -> None:
        self.ensure_history_visible(ctx, member)
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
                f"{discord.utils.format_dt(r['created_at'], 'R')}  |  {discord.utils.format_dt(r['created_at'], 'd')} | `ID: {r['id']}`",
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Nicknames names for {member}"
        # source.embed.description = f"-# View all nicknames [here](https://crygup.com/discord?tab=user&q={member.id})"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="discrims", aliases=("discriminators",))
    async def discrims(self, ctx: Context, *, member: discord.Member = commands.Author):
        """Shows a user's previous discrims"""

        await self._discrims(ctx, member)

    async def _discrims(
        self, ctx: Context, member: discord.User | discord.Member
    ) -> None:
        """Render a user's discriminator history for another command."""

        self.ensure_history_visible(ctx, member)
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
                f"{discord.utils.format_dt(r['created_at'], 'R')}  |  {discord.utils.format_dt(r['created_at'], 'd')} | `ID: {r['id']}`",
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.title = f"Discriminators names for {member}"
        source.embed.description = f"-# View all discriminators [here](https://crygup.com/discord?tab=user&subtab=discrims&q={member.id})"
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
                f"{discord.utils.format_dt(r['created_at'], 'R')}  |  {discord.utils.format_dt(r['created_at'], 'd')} | `ID: {r['id']}`",
            )
            for r in results
        ]

        source = FieldPageSource(entries=entries)
        source.embed.color = self.bot.embedcolor
        source.embed.description = f"-# View all server names [here](https://crygup.com/discord?tab=guild&subtab=names&q={guild.id})"
        source.embed.title = f"Names for {guild}"
        pager = Pager(source, ctx=ctx)
        await pager.start(ctx)

    @commands.command(name="icons")
    async def icons(
        self, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild
    ):
        """Shows a server's previous icons"""

        await self.icons_func(ctx, guild)

    async def icons_func(self, ctx: Context, guild: discord.Guild) -> None:
        """Show a server's saved icons as a paginated list."""

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
            source.embed.description = f"-# View all icons [here](https://crygup.com/discord?tab=guild&subtab=icons&q={guild.id})"
            pager = Pager(source, ctx=ctx)
            await pager.start(ctx)

    async def icons_grid(
        self,
        ctx: Context,
        guild: discord.Guild,
        grid_size: tuple[int, int] | None = None,
    ) -> None:
        """Show saved server icons in a grid."""
        xbound, ybound = grid_size or (0, 0)
        record_limit = xbound * ybound if grid_size else 100
        records: List[asyncpg.Record] = await self.bot.pool.fetch(
            "SELECT * FROM guild_icons WHERE guild_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            guild.id,
            record_limit,
        )
        if not records:
            raise commands.BadArgument(f"{guild} has no icons on record.")

        urls = [record["icon"] for record in records]
        refreshed = [
            url
            for chunk in utils.as_chunks(urls, max_size=50)
            for url in await self.refresh_urls(chunk)
        ]
        images = await asyncio.gather(
            *[to_image(ctx.session, url, bytes=True) for url in refreshed]
        )
        file = discord.File(
            await format_bytes(
                ctx.guild.filesize_limit if ctx.guild else 8_388_608,
                images,  # type: ignore[arg-type]
                xbound=xbound,
                ybound=ybound,
            ),
            f"{guild.id}_icon_history.png",
        )
        embed = discord.Embed(color=self.bot.embedcolor)
        embed.set_author(name=f"{guild}'s icons in a grid view.")
        embed.set_image(url=f"attachment://{file.filename}")
        embed.set_footer(text="First icon saved")
        await ctx.send(file=file, embed=embed)

    @commands.command(name="uptime")
    async def uptime(self, ctx: GuildContext, *, user: Optional[discord.User] = None):
        """Shows how long the bot has been online, or a user's last seen status"""
        if user is None:
            if self.bot.user:
                await ctx.send(
                    f"Hi, I have been awake for {human_timedelta(self.bot.start_time, suffix=False)}"
                )
            return

        self.ensure_history_visible(ctx, user)
        row = None

        if ctx.guild:
            row = await self.bot.pool.fetchrow(
                "SELECT status, last_seen FROM user_statuses WHERE user_id = $1 AND guild_id = $2 ORDER BY last_seen DESC LIMIT 1",
                user.id,
                ctx.guild.id,
            )

        if row is None:
            row = await self.bot.pool.fetchrow(
                "SELECT status, last_seen FROM user_statuses WHERE user_id = $1 ORDER BY last_seen DESC LIMIT 1",
                user.id,
            )

        if row is None:
            await ctx.send(f"I haven't seen {user} online yet.")
            return

        status = row["status"]
        last_seen = row["last_seen"]
        delta = human_timedelta(last_seen, suffix=False)
        status_nice = status if status != "***dnd***" else "on ***Do Not Disturb***"
        await ctx.send(f"{user} was last seen {status_nice} {delta} ago.")

    @commands.command(
        name="statuscalendar",
        aliases=("statuscal", "statushistory", "statuses", "status"),
    )
    async def status_calendar(
        self,
        ctx: GuildContext,
        *,
        user: Optional[discord.Member] = None,
    ):
        """Shows a member's daily status activity over the last 31 days."""
        async with ctx.typing():
            await self._status_calendar(ctx, user)

    async def _status_calendar(
        self,
        ctx: GuildContext,
        user: Optional[discord.Member] = None,
    ) -> None:
        started = time.monotonic()
        target = user or ctx.author
        self.ensure_history_visible(ctx, target)
        now = discord.utils.utcnow()
        cutoff = now - datetime.timedelta(days=31)
        rows = await self.bot.pool.fetch(
            """
            SELECT status, started_at, ended_at
            FROM user_status_history
            WHERE user_id = $1
              AND guild_id = $2
              AND started_at <= $3
              AND COALESCE(ended_at, $3) >= $4
            ORDER BY started_at
            """,
            target.id,
            ctx.guild.id,
            now,
            cutoff,
        )
        if not rows or not any(row["started_at"] >= cutoff for row in rows):
            raise commands.BadArgument(
                f"I have no status activity recorded for {target} in the last 31 days."
            )

        intervals = [
            StatusInterval(
                status=row["status"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
            )
            for row in rows
        ]
        start_date = now.date() - datetime.timedelta(days=30)
        statuses = hourly_statuses(
            intervals,
            start_date,
            now=now,
        )
        image = await asyncio.to_thread(
            render_status_calendar,
            target.display_name,
            start_date,
            statuses,
        )
        filename = "status-calendar.png"
        gallery = discord.ui.MediaGallery(
            discord.MediaGalleryItem(f"attachment://{filename}")
        )
        details = discord.ui.TextDisplay(
            f"-# Invoked by {ctx.author.mention}\n"
            f"-# Took {time.monotonic() - started:.1f}s"
        )
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                "## "
                f"{utils.escape_mentions(utils.escape_markdown(target.display_name))}"
                "'s status calendar"
            ),
            gallery,
            details,
            accent_color=self.bot.embedcolor,
        )
        view_type = type("StatusCalendarView", (discord.ui.LayoutView,), {})
        view = view_type(timeout=None)
        view.add_item(container)
        await ctx.send(
            file=discord.File(image, filename),
            view=view,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )

    @cast(Any, commands.hybrid_group)(
        name="joins",
        invoke_without_command=True,
        fallback="user",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def joins(self, ctx: Context, *, user: discord.User = commands.Author):
        """See your or another user's join stats."""
        await self._joins_user_stats(ctx, user)

    @joins.command(name="leaderboard", aliases=["lb", "top"])
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def joins_leaderboard(
        self, ctx: Context, *, server: Optional[discord.Guild] = commands.CurrentGuild
    ):
        """Join leaderboard for this or another server."""
        if server is None:
            raise commands.BadArgument("No server specified and not in a server.")
        await self._joins_server_leaderboard(ctx, server)

    @joins.command(name="global")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def joins_global(self, ctx: Context):
        """Global join leaderboard across all servers."""
        rows = await ctx.bot.pool.fetch(
            "SELECT member_id, COUNT(*) AS total FROM member_join_logs "
            "GROUP BY member_id ORDER BY total DESC"
        )
        if not rows:
            await ctx.send("No join data yet!")
            return
        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(name="Join Leaderboard  •  Global")
        lines = []
        for r in rows:
            member_id = int(r["member_id"])
            user = await get_or_fetch_user(ctx.bot, member_id)
            if user.bot:
                self.bot.db_cache.remember_user(user.id, is_bot=True)
            if member_id != ctx.author.id and (
                not user.bot
                and (
                    not self.bot.db_cache.user_history_is_public(member_id)
                    or self.bot.db_cache.user_tracking_opted_out(member_id, "joins")
                )
            ):
                continue
            name = user.name if user else str(member_id)
            lines.append(f"**{r['total']:,}** {name}")
            if len(lines) == 10:
                break
        if not lines:
            await ctx.send("No public join data yet!")
            return
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    async def _joins_server_leaderboard(
        self, ctx: Context, guild: discord.Guild
    ) -> None:
        rows = await ctx.bot.pool.fetch(
            "SELECT member_id, COUNT(*) AS total FROM member_join_logs "
            "WHERE guild_id = $1 GROUP BY member_id ORDER BY total DESC",
            guild.id,
        )
        if not rows:
            await ctx.send(f"No join data for **{guild.name}** yet!")
            return
        embed = discord.Embed(color=ctx.bot.embedcolor)
        embed.set_author(
            name=f"Join Leaderboard  •  {guild.name}",
            icon_url=guild.icon.url if guild.icon else None,
        )
        lines = []
        for r in rows:
            member_id = int(r["member_id"])
            user = guild.get_member(member_id) or await get_or_fetch_user(
                ctx.bot, member_id
            )
            if user.bot:
                self.bot.db_cache.remember_user(user.id, is_bot=True)
            if member_id != ctx.author.id and (
                not user.bot
                and (
                    not self.bot.db_cache.user_history_is_public(member_id)
                    or self.bot.db_cache.user_tracking_opted_out(member_id, "joins")
                )
            ):
                continue
            name = user.name if user else str(member_id)
            lines.append(f"**{r['total']:,}** {name}")
            if len(lines) == 10:
                break
        if not lines:
            await ctx.send(f"No public join data for **{guild.name}** yet!")
            return
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    async def _joins_user_stats(self, ctx: Context, user: discord.User) -> None:
        self.ensure_history_visible(ctx, user)

        guild_total = (
            await ctx.bot.pool.fetchval(
                "SELECT COUNT(*) FROM member_join_logs WHERE member_id = $1 AND guild_id = $2",
                user.id,
                ctx.guild.id,
            )
            if ctx.guild
            else 0
        )
        global_total = (
            await ctx.bot.pool.fetchval(
                "SELECT COUNT(*) FROM member_join_logs WHERE member_id = $1",
                user.id,
            )
            or 0
        )

        if not guild_total and not global_total:
            if (
                ctx.guild
                and self.bot.logging
                and not self.bot.db_cache.user_tracking_opted_out(user.id, "joins")
            ):
                member = ctx.guild.get_member(user.id)
                if member is not None:
                    await self.bot.logging.add_join(member)
                    guild_total = 1
                    global_total = 1
                else:
                    await ctx.send(f"**{user.name}** has no join records yet!")
                    return
            else:
                await ctx.send(f"**{user.name}** has no join records yet!")
                return

        guild_name = ctx.guild.name if ctx.guild else "this server"
        await ctx.send(
            f"**{utils.escape_markdown(user.name)}** has joined {guild_name} "
            f"{plural(int(guild_total)):time}.\n-# *{plural(int(global_total)):join} across all servers*"
        )
