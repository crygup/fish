from __future__ import annotations

import asyncio
import datetime
import mimetypes
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, cast
from urllib.parse import urlsplit

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
    TemporaryMediaError,
    fetch_public_bytes,
    format_bytes,
    get_or_fetch_user,
    human_timedelta,
    plural,
    to_image,
    upload_temporary_media,
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
    merged_hourly_statuses,
    render_status_calendar,
)

AVATAR_GRID_RE = re.compile(
    r"^(?P<width>\d{1,2})\s*x\s*(?P<height>\d{1,2})$", re.IGNORECASE
)
AVATAR_EXPORT_LIMIT = 1_000
AVATAR_EXPORT_FETCH_CONCURRENCY = 5
AVATAR_EXPORT_BATCH_SIZE = 5
AVATAR_EXPORT_MAX_IMAGE_BYTES = 20 * 1024 * 1024
DISCORD_DM_FILE_LIMIT = 8 * 1024 * 1024

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

    @commands.command(name="activity", aliases=("playing",))
    @commands.guild_only()
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
        self,
        ctx: Context,
        user: discord.User | discord.Member,
        guild_id: Optional[int] = None,
        *,
        edit: bool = False,
    ):
        self.ensure_history_visible(ctx, user)
        if edit and user.id != ctx.author.id:
            raise commands.BadArgument(
                "You can only edit your own saved avatar history."
            )
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
                + (" • Edit Mode" if edit else "")
            )
            source.embed.description = f"-# View all avatars [here](https://crygup.com/discord?tab=user&subtab=avatars&q={user.id})"

            async def delete_avatar(page_number: int) -> bool:
                if not 0 <= page_number < len(entries):
                    return False
                record_id = entries[page_number][2]
                if guild_id is not None:
                    result = await self.bot.pool.execute(
                        "DELETE FROM guild_avatars "
                        "WHERE id = $1 AND member_id = $2 AND guild_id = $3",
                        record_id,
                        ctx.author.id,
                        guild_id,
                    )
                else:
                    result = await self.bot.pool.execute(
                        "DELETE FROM avatars WHERE id = $1 AND user_id = $2",
                        record_id,
                        ctx.author.id,
                    )
                return not result.endswith(" 0")

            def delete_prompt(page_number: int) -> str:
                record_id = entries[page_number][2]
                return (
                    f"Delete saved avatar ID `{record_id}` from Fishie's database? "
                    "This cannot be undone."
                )

            pager = Pager(
                source,
                ctx=ctx,
                delete_page=delete_avatar if edit else None,
                delete_prompt=delete_prompt if edit else None,
            )
            await pager.start(ctx)

    @staticmethod
    async def _avatar_list_arguments(
        ctx: Context,
        arguments: tuple[str, ...],
    ) -> tuple[discord.User | discord.Member, bool]:
        edit = False
        user_arguments: list[str] = []
        for argument in arguments:
            if argument.strip().casefold() == "edit":
                edit = True
            else:
                user_arguments.append(argument)

        if not user_arguments:
            return ctx.author, edit
        raw_user = " ".join(user_arguments).strip()
        try:
            user = await commands.UserConverter().convert(ctx, raw_user)
        except commands.UserNotFound as error:
            raise commands.BadArgument(
                f"Could not find a user matching {raw_user}."
            ) from error
        return user, edit

    @staticmethod
    async def _avatar_export_arguments(
        ctx: Context,
        arguments: tuple[str, ...],
    ) -> tuple[discord.User | discord.Member, int | None]:
        """Parse an order-independent export user and server scope."""

        use_server = False
        user_arguments: list[str] = []
        for argument in arguments:
            if argument.strip().casefold() in {"server", "guild"}:
                use_server = True
            else:
                user_arguments.append(argument)

        if use_server and ctx.guild is None:
            raise commands.NoPrivateMessage(
                "Server avatar exports can only be requested in a server."
            )

        if user_arguments:
            raw_user = " ".join(user_arguments).strip()
            try:
                user = await commands.UserConverter().convert(ctx, raw_user)
            except commands.UserNotFound as error:
                raise commands.BadArgument(
                    f"Could not find a user matching {raw_user}."
                ) from error
        else:
            user = ctx.author

        return user, ctx.guild.id if use_server and ctx.guild else None

    async def _refresh_avatar_export_urls(self, urls: list[str]) -> list[str]:
        """Refresh Discord CDN URLs in API-sized batches, preserving order."""

        if not urls:
            return []
        mapping: dict[str, str] = {}
        for chunk in utils.as_chunks(urls, max_size=50):
            originals = [str(url).split("?", 1)[0] for url in chunk if url]
            if not originals:
                continue
            try:
                response = await self.bot.http.request(
                    Route("POST", "/attachments/refresh-urls"),
                    json={"attachment_urls": originals},
                )
            except (discord.HTTPException, OSError):
                # Keep the existing URL; it may still be valid, and the
                # bounded fetch below will report an individual failure.
                continue
            if not isinstance(response, dict):
                continue
            for item in response.get("refreshed_urls", []):
                if not isinstance(item, dict):
                    continue
                original = str(item.get("original") or "").split("?", 1)[0]
                refreshed = str(item.get("refreshed") or "")
                if original and refreshed:
                    mapping[original] = refreshed

        return [mapping.get(str(url).split("?", 1)[0], str(url)) for url in urls]

    @staticmethod
    def _avatar_export_filename(
        record: asyncpg.Record,
        url: str,
        content_type: str,
    ) -> str:
        """Build a stable, filesystem-safe ``ID_DATE.extension`` name."""

        created_at = record["created_at"]
        if isinstance(created_at, datetime.datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=datetime.timezone.utc)
            date = created_at.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d")
        else:
            date = "unknown-date"

        extension = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ""
        if extension == ".jpe":
            extension = ".jpg"
        if extension not in {".gif", ".jpg", ".jpeg", ".png", ".webp"}:
            try:
                extension = Path(urlsplit(url).path).suffix.casefold()
            except ValueError:
                extension = ""
        if extension == ".jpe":
            extension = ".jpg"
        if extension not in {".gif", ".jpg", ".jpeg", ".png", ".webp"}:
            extension = ".bin"
        return f"{int(record['id'])}_{date}{extension}"

    @commands.group(
        name="avatars",
        aliases=("pfps", "avis", "avs"),
        extras={"usage": "[user] [edit]"},
        invoke_without_command=True,
    )
    async def avatars(self, ctx: Context, *arguments: str):
        """Shows a user's previous avatars"""

        user, edit = await self._avatar_list_arguments(ctx, arguments)
        await self.avatars_func(ctx, user, edit=edit)

    @avatars.command(
        name="download",
        aliases=("export",),
        extras={"usage": "[user] [server|guild]"},
    )
    @commands.cooldown(1, 24 * 60 * 60, commands.BucketType.user)
    async def avatars_download(self, ctx: Context, *arguments: str) -> None:
        """Export a user's saved avatars as a ZIP archive."""

        user, guild_id = await self._avatar_export_arguments(ctx, arguments)
        self.ensure_history_visible(ctx, user)

        if guild_id is not None:
            records = await self.bot.pool.fetch(
                "SELECT id, avatar, created_at FROM guild_avatars "
                "WHERE member_id = $1 AND guild_id = $2 "
                "ORDER BY created_at DESC, id DESC LIMIT $3",
                user.id,
                guild_id,
                AVATAR_EXPORT_LIMIT + 1,
            )
        else:
            records = await self.bot.pool.fetch(
                "SELECT id, avatar, created_at FROM avatars "
                "WHERE user_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2",
                user.id,
                AVATAR_EXPORT_LIMIT + 1,
            )

        if not records:
            scope = "server avatars" if guild_id is not None else "avatars"
            raise commands.BadArgument(f"{user} has no {scope} on record.")

        truncated = len(records) > AVATAR_EXPORT_LIMIT
        records = list(records[:AVATAR_EXPORT_LIMIT])
        progress = await ctx.send(
            f"Preparing an export of **{discord.utils.escape_markdown(user.name)}** "
            f"({len(records):,} avatar{'' if len(records) == 1 else 's'})...\n"
            "0% complete",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        urls = await self._refresh_avatar_export_urls(
            [str(record["avatar"]) for record in records]
        )
        semaphore = asyncio.Semaphore(AVATAR_EXPORT_FETCH_CONCURRENCY)
        completed = 0
        downloaded = 0
        failed = 0
        last_progress = 0.0
        used_names: set[str] = set()

        async def fetch_avatar(url: str) -> Any:
            async with semaphore:
                return await fetch_public_bytes(
                    ctx.session,
                    url,
                    max_bytes=AVATAR_EXPORT_MAX_IMAGE_BYTES,
                    allowed_content_prefixes=("image/",),
                )

        async def update_progress(*, force: bool = False) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if not force and completed < len(records) and now - last_progress < 1:
                return
            last_progress = now
            percent = int(completed * 100 / len(records))
            try:
                await progress.edit(
                    content=(
                        f"Preparing an export of **{discord.utils.escape_markdown(user.name)}**...\n"
                        f"{percent}% complete"
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                self.bot.logger.debug(
                    "Could not update avatar export progress", exc_info=True
                )

        with tempfile.TemporaryDirectory(prefix=".pfps-export-") as directory:
            archive_path = Path(directory) / f"avatars_{user.id}.zip"
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for start in range(0, len(records), AVATAR_EXPORT_BATCH_SIZE):
                    batch = records[start : start + AVATAR_EXPORT_BATCH_SIZE]
                    results = await asyncio.gather(
                        *(
                            fetch_avatar(url)
                            for url in urls[start : start + len(batch)]
                        ),
                        return_exceptions=True,
                    )
                    batch_urls = urls[start : start + len(batch)]
                    for record, url, result in zip(batch, batch_urls, results):
                        completed += 1
                        if isinstance(result, BaseException):
                            failed += 1
                            self.bot.logger.debug(
                                "Could not export avatar %s for user %s: %s",
                                record["id"],
                                user.id,
                                result,
                            )
                            continue
                        filename = self._avatar_export_filename(
                            record, url, result.content_type
                        )
                        if filename in used_names:
                            stem, suffix = Path(filename).stem, Path(filename).suffix
                            filename = f"{stem}_{record['id']}{suffix}"
                        used_names.add(filename)
                        archive.writestr(filename, result.data)
                        downloaded += 1
                    await update_progress()

            await update_progress(force=True)
            if not downloaded:
                await progress.edit(
                    content="None of the saved avatars could be downloaded.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            archive_size = archive_path.stat().st_size
            discord_limit = (
                ctx.guild.filesize_limit if ctx.guild else DISCORD_DM_FILE_LIMIT
            )
            summary = (
                f"Exported **{downloaded:,}/{len(records):,}** avatars"
                + (f" ({failed:,} unavailable)" if failed else "")
                + (" · Limited to the newest 1,000" if truncated else "")
            )
            archive_name = f"avatars_{user.id}.zip"
            if archive_size <= discord_limit:
                await progress.edit(
                    content=summary,
                    attachments=[discord.File(archive_path, filename=archive_name)],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            try:
                hosted_url = await upload_temporary_media(
                    self.bot,
                    archive_path,
                    archive_name,
                    content_type="application/octet-stream",
                )
            except TemporaryMediaError as error:
                await progress.edit(
                    content=(
                        f"{summary}\nThe ZIP is too large to send here, and temporary "
                        "hosting was unavailable."
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                raise commands.BadArgument(
                    "The avatar ZIP exceeded both Discord's and temporary hosting's limits."
                ) from error

            await progress.edit(
                content=(
                    f"{summary}\n[Download the avatar export]({hosted_url})\n"
                    "-# This download link expires in 30 minutes."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )

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

    @avatars.command(
        name="server",
        aliases=("guild", "s"),
        extras={"usage": "[user] [edit]"},
    )
    @commands.guild_only()
    async def server_avatars(self, ctx: GuildContext, *arguments: str):
        """Shows a member's previous server avatars"""

        user, edit = await self._avatar_list_arguments(ctx, arguments)
        await self.avatars_func(ctx, user, ctx.guild.id, edit=edit)

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

    @commands.command(name="icons", extras={"usage": "[server] [edit]"})
    async def icons(self, ctx: Context, *arguments: str):
        """Shows a server's previous icons"""

        edit = False
        guild_arguments: list[str] = []
        for argument in arguments:
            if argument.strip().casefold() == "edit":
                edit = True
            else:
                guild_arguments.append(argument)
        if guild_arguments:
            guild = await commands.GuildConverter().convert(
                ctx, " ".join(guild_arguments)
            )
        elif ctx.guild is not None:
            guild = ctx.guild
        else:
            raise commands.NoPrivateMessage(
                "A server must be provided when using this command in DMs."
            )
        await self.icons_func(ctx, guild, edit=edit)

    async def icons_func(
        self,
        ctx: Context,
        guild: discord.Guild,
        *,
        edit: bool = False,
    ) -> None:
        """Show a server's saved icons as a paginated list."""

        if edit:
            member = guild.get_member(ctx.author.id)
            if member is None or not member.guild_permissions.manage_guild:
                raise commands.MissingPermissions(["manage_guild"])

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
            source.embed.title = f"Icons for {guild}" + (" • Edit Mode" if edit else "")
            source.embed.description = f"-# View all icons [here](https://crygup.com/discord?tab=guild&subtab=icons&q={guild.id})"

            async def delete_icon(page_number: int) -> bool:
                if not 0 <= page_number < len(entries):
                    return False
                record_id = entries[page_number][2]
                result = await self.bot.pool.execute(
                    "DELETE FROM guild_icons WHERE id = $1 AND guild_id = $2",
                    record_id,
                    guild.id,
                )
                return not result.endswith(" 0")

            def delete_prompt(page_number: int) -> str:
                record_id = entries[page_number][2]
                return (
                    f"Delete saved server icon ID `{record_id}` from Fishie's "
                    "database? This cannot be undone."
                )

            pager = Pager(
                source,
                ctx=ctx,
                delete_page=delete_icon if edit else None,
                delete_prompt=delete_prompt if edit else None,
            )
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
            SELECT guild_id, status, started_at, ended_at
            FROM user_status_history
            WHERE user_id = $1
              AND started_at <= $2
              AND COALESCE(ended_at, $2) >= $3
            ORDER BY guild_id, started_at
            """,
            target.id,
            now,
            cutoff,
        )
        if not rows:
            raise commands.BadArgument(
                f"I have no status activity recorded for {target} in the last 31 days."
            )

        intervals_by_guild: dict[int, list[StatusInterval]] = {}
        for row in rows:
            intervals_by_guild.setdefault(int(row["guild_id"]), []).append(
                StatusInterval(
                    status=row["status"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                )
            )
        start_date = now.date() - datetime.timedelta(days=30)
        statuses = merged_hourly_statuses(
            intervals_by_guild,
            ctx.guild.id,
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
