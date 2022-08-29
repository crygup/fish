from __future__ import annotations

from typing import Dict, Optional

import discord
from bot import Bot, Context
from discord.ext import commands
from discord.utils import remove_markdown
from utils import (
    LastfmClient,
    LastfmConverter,
    LastfmTimeConverter,
    SimplePages,
    get_lastfm,
    lastfm_period,
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

    @commands.command(name="cover", aliases=("co",))
    async def cover(self, ctx: Context, *, query: Optional[str]):
        """Gets the album cover for the last played track"""
        if query is None:
            user = await get_lastfm(ctx.bot, ctx.author.id)
            info = await LastfmClient(
                self.bot, "2.0", "user.getrecenttracks", "user", user
            )

            if info["recenttracks"]["track"] == []:
                raise TypeError("No recent tracks found for this user.")

            track = info["recenttracks"]["track"][0]
            title = f'**{track["artist"]["#text"]}** - **[{track["album"]["#text"]}]({track["url"]})**'
            if track["image"][3]["#text"] == "":
                raise TypeError(f"No album cover found for {title}")
            image = await ctx.to_bytesio(track["image"][3]["#text"])
        else:
            info = await LastfmClient(self.bot, "2.0", "album.search", "album", query)
            if info["results"]["albummatches"]["album"] == []:
                raise TypeError("No album found for this query.")
            track = info["results"]["albummatches"]["album"][0]
            title = f'**{track["artist"]}** - **[{track["name"]}]({track["url"]})**'
            if track["image"][3]["#text"] == "":
                raise TypeError(
                    f'No album cover found for `{track["artist"]} - {track["name"]}`'
                )

            image = await ctx.to_bytesio(track["image"][3]["#text"])

        embed = discord.Embed(description=title)
        embed.set_image(url="attachment://cover.png")
        await ctx.send(
            embed=embed,
            file=discord.File(fp=image, filename="cover.png"),
            check_ref=True,
        )

    # add time parameter? weekly, monthly, yearly, etc
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

        results = await LastfmClient(
            self.bot,
            "2.0",
            "user.gettopalbums",
            "user",
            name,
            extras=f"&limit=200&period={period}",
        )
        if results["topalbums"]["album"] == []:
            raise TypeError("No recent tracks found for this user.")

        info = results["topalbums"]
        data = [
            f"**{i['artist']['name']}** - **[{i['name']}]({i['url']})** ({int(i['playcount']):,} plays)"
            for i in info["album"]
        ]
        pages = SimplePages(entries=data, per_page=10, ctx=ctx)
        pages.embed.title = f"Top {lastfm_period[period]} albums for {name}"
        pages.embed.color = self.bot.embedcolor
        await pages.start(ctx)
