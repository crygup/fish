from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from discord.ext import commands

from utils import (
    LastfmConverter,
    LastfmTimeConverter,
    Pager,
    SimplePages,
    get_lastfm,
    get_lastfm_data,
    lastfm_period,
    BaseCog,
)
from .functions import *

if TYPE_CHECKING:
    from cogs.context import Context


class TopCommands(BaseCog):
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
