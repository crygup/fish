from __future__ import annotations

import asyncio
import datetime
import textwrap
from typing import TYPE_CHECKING, Annotated, List, Optional

import discord
from discord.ext import commands
from discord.utils import remove_markdown

from utils import (
    LastfmConverter,
    LastfmTimeConverter,
    NoCover,
    SimplePages,
    format_bytes,
    get_lastfm,
    get_lastfm_data,
    get_sp_cover,
    lastfm_period,
    to_bytes,
    BlankException,
)

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(LastFm(bot))


class LastFm(commands.Cog, name="lastfm"):
    """Last.fm integration"""

    def __init__(self, bot: Bot):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="lastfm", id=1006848047923351612)

    @commands.group(name="lastfm", aliases=("fm",), invoke_without_command=True)
    async def last_fm(self, ctx: Context, username: LastfmConverter = commands.Author):
        """Displays your last scrobble from last.fm"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        try:
            track = (
                await self.bot.lastfm.fetch_user_recent_tracks(
                    user=name, extended=True, limit=1
                )
            )[0]
        except IndexError:
            raise BlankException(f"{name} has no recent tracks.")

        description = f"""
        **Artist**: {track.artist.name}
        **Track**: {track.name}
        **Album**: {track.album.name}
        """

        loved = f"\U00002764\U0000fe0f " if track.loved else ""
        footer_text = f"{loved}{name} has {track.attr.total_scrobbles:,} scrobbles"

        if track.played_at:
            footer_text += "\nLast scrobble"

        embed = discord.Embed(
            color=self.bot.embedcolor,
            description=textwrap.dedent(description),
            timestamp=track.played_at.replace(tzinfo=datetime.timezone.utc)
            if track.played_at
            else None,
        )

        embed.set_author(
            name=f"{'Now playing -' if track.now_playing else 'Last track for - '} {name}",
            url=track.url,
            icon_url=ctx.author.display_avatar.url,
        )

        embed.set_thumbnail(url=track.images.extra_large or track.images.large)
        embed.set_footer(text=footer_text)
        await ctx.send(embed=embed, check_ref=True)

    @last_fm.command(name="set")
    async def lastfm_set(self, ctx: Context, username: str):
        """Alias for set lastfm command"""

        command = self.bot.get_command("set lastfm")

        if command is None:
            return

        await command(ctx, username=username)

    @commands.command(name="toptracks", aliases=("tt",))
    async def top_tracks(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(converter=LastfmTimeConverter, default="7day"),
    ):
        """Gets top tracks"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await get_lastfm_data(
            self.bot,
            "2.0",
            "user.gettoptracks",
            "user",
            name,
            extras={"limit": 200, "period": period},
        )
        if results["toptracks"]["track"] == []:
            raise TypeError("No recent tracks found for this user.")

        info = results["toptracks"]
        data = [
            f"**{i['artist']['name']}** - **[{i['name']}]({i['url']})** ({int(i['playcount']):,} plays)"
            for i in info["track"]
        ]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Top {lastfm_period[period]} tracks for {name}"
        pages.embed.color = self.bot.embedcolor
        await pages.start(ctx)

    @commands.command(name="topartists", aliases=("ta",))
    async def top_artists(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(converter=LastfmTimeConverter, default="7day"),
    ):
        """Gets top artists"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await get_lastfm_data(
            self.bot,
            "2.0",
            "user.gettopartists",
            "user",
            name,
            extras={"limit": 200, "period": period},
        )
        if results["topartists"]["artist"] == []:
            raise TypeError("No recent tracks found for this user.")

        info = results["topartists"]
        data = [
            f"**[{i['name']}]({i['url']})** ({int(i['playcount']):,} plays)"
            for i in info["artist"]
        ]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Top {lastfm_period[period]} artists for {name}"
        pages.embed.color = self.bot.embedcolor
        await pages.start(ctx)

    @commands.command(
        name="topalbums",
        aliases=(
            "talb",
            "tab",
        ),
    )
    async def top_albums(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(converter=LastfmTimeConverter, default="7day"),
    ):
        """Gets top albums"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await get_lastfm_data(
            self.bot,
            "2.0",
            "user.gettopalbums",
            "user",
            name,
            extras={"limit": 200, "period": period},
        )
        if results["topalbums"]["album"] == []:
            raise TypeError("No tracks found for this user.")

        info = results["topalbums"]
        data = [
            f"**{i['artist']['name']}** - **[{i['name']}]({i['url']})** ({int(i['playcount']):,} plays)"
            for i in info["album"]
        ]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Top {lastfm_period[period]} albums for {name}"
        pages.embed.color = self.bot.embedcolor
        await pages.start(ctx)

    @commands.command(name="chart", aliases=("c",))
    async def chart(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(converter=LastfmTimeConverter, default="7day"),
    ):
        """Makes a 3x3 image of your top albums"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await get_lastfm_data(
            self.bot,
            "2.0",
            "user.gettopalbums",
            "user",
            name,
            extras={"limit": 100, "period": period},
        )

        data = results["topalbums"].get("album")

        if data == [] or data is None:
            raise TypeError("No tracks found for this user.")

        urls = []

        total = 0
        chart_nsfw = False
        for track in data:
            if total == 9:
                break
            try:
                url, nsfw = await get_sp_cover(
                    self.bot, f"{track['name']} artist:{track['artist']['name']}"
                )
                urls.append(url)
                if nsfw:
                    chart_nsfw = True
                total += 1
            except (IndexError, NoCover):
                continue

        images: List[bytes] = await asyncio.gather(
            *[to_bytes(ctx.session, url) for url in urls]
        )
        fp = await format_bytes(ctx.guild.filesize_limit, images)
        file = discord.File(fp, f"{name}_chart.png", spoiler=chart_nsfw)

        await ctx.send(file=file)
