from __future__ import annotations

import asyncio
from typing import Dict, List
from typing import Literal as L
from typing import Optional, Union

import discord
from bot import Bot, Context
from discord.ext import commands
from discord.utils import remove_markdown
from typing_extensions import reveal_type
from utils import (
    LastfmClient,
    LastfmConverter,
    LastfmTimeConverter,
    SimplePages,
    format_bytes,
    get_lastfm,
    lastfm_period,
    url_to_bytes,
    get_sp_cover,
    NoCover,
)


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

        async with ctx.typing():
            info = await LastfmClient(
                self.bot, "2.0", "user.getrecenttracks", "user", name
            )

            if info["recenttracks"]["track"] == []:
                raise TypeError("No recent tracks found for this user.")

            track = info["recenttracks"]["track"][0]
            user = info["recenttracks"]["@attr"]["user"]
            scrobbles = f"{int(info['recenttracks']['@attr']['total']):,}"
            playing = "Now playing" if not track.get("date") else "Last track for"
            artist = track["artist"]["#text"]
            name = track["name"]
            album = track["album"]["#text"]

            scrobbled = (
                f'\n**Scrobbled**: <t:{track["date"]["uts"]}:R>'
                if track.get("date")
                else ""
            )
            embed = discord.Embed(
                description=f"**Artist**: {remove_markdown(artist)} \n**Track**: {remove_markdown(name)} \n**Album**: {remove_markdown(album)} {scrobbled}",
            )
            embed.set_thumbnail(url=track["image"][3]["#text"])
            embed.set_author(
                name=f"{playing} - {remove_markdown(user)}",
                url=track["url"],
                icon_url=ctx.author.display_avatar.url,
            )
            embed.set_footer(text=f"{user} has {scrobbles} scrobbles")

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
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="overall"
        ),
    ):
        """Gets top tracks"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await LastfmClient(
            self.bot,
            "2.0",
            "user.gettoptracks",
            "user",
            name,
            extras=f"&limit=200&period={period}",
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
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="overall"
        ),
    ):
        """Gets top artists"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await LastfmClient(
            self.bot,
            "2.0",
            "user.gettopartists",
            "user",
            name,
            extras=f"&limit=200&period={period}",
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
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="overall"
        ),
    ):
        """Gets top albums"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await LastfmClient(
            self.bot,
            "2.0",
            "user.gettopalbums",
            "user",
            name,
            extras=f"&limit=200&period={period}",
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
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="overall"
        ),
    ):
        """Makes a 3x3 image of your top albums"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        await ctx.trigger_typing()
        results = await LastfmClient(
            self.bot,
            "2.0",
            "user.gettopalbums",
            "user",
            name,
            extras=f"&limit=100&period={period}",
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
            *[url_to_bytes(ctx, url) for url in urls]
        )
        fp = await format_bytes(ctx.guild.filesize_limit, images)
        file = discord.File(fp, f"{name}_chart.png", spoiler=chart_nsfw)

        await ctx.send(file=file)
