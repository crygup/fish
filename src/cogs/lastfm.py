from __future__ import annotations

import asyncio
import datetime
import itertools
import textwrap
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from utils import (
    BlankException,
    LastfmConverter,
    LastfmTimeConverter,
    NoCover,
    SimplePages,
    get_lastfm,
    get_lastfm_data,
    get_sp_cover,
    lastfm_period,
    to_bytesio,
    to_thread,
    shorten,
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
        **Artist**: {discord.utils.escape_markdown(track.artist.name)}
        **Track**: {discord.utils.escape_markdown(track.name)}
        **Album**: {discord.utils.escape_markdown(track.album.name)}
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
            name=f"{'Now playing -' if track.now_playing else 'Last track for - '} {discord.utils.escape_markdown(name)}",
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
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="weekly"
        ),
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
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="weekly"
        ),
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
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="weekly"
        ),
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

    @to_thread
    def make_chart(self, data: List[Tuple[BytesIO, str]], name: str):
        # fmt: off
        image_cords = itertools.chain(
            [(100, 100), (400, 100), (700, 100), (100, 400), (400, 400), (700, 400), (100, 700), (400, 700), (700, 700),]
        )
        spacing = 20
        # fmt: on
        font = ImageFont.truetype("src/files/assets/fonts/wsr.otf", 25)
        name_font = ImageFont.truetype("src/files/assets/fonts/wsr.otf", 50)
        output_buffer = BytesIO()
        with Image.open("src/files/assets/chart.png") as image:
            draw = ImageDraw.Draw(image)

            text_width, _ = draw.textsize(name, font=name_font)
            text_x = 500 - text_width // 2
            draw.text((text_x, 30), name, fill=(255, 255, 255), font=name_font)

            for item in data:
                with Image.open(item[0]) as cover:
                    cover = cover.resize((200, 200))
                    x, y = next(image_cords)
                    image.paste(cover, (x, y))
                    draw.text(
                        (x, y + 200 + spacing), item[1], font=font, fill=(255, 255, 255)
                    )

            image.save(output_buffer, "png")
            output_buffer.seek(0)

        return output_buffer

    @commands.command(name="chart", aliases=("c",))
    async def chart(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(
            converter=LastfmTimeConverter, default="weekly"
        ),
    ):
        """View your top albums"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        async with ctx.typing():
            results = await get_lastfm_data(
                self.bot,
                "2.0",
                "user.gettopalbums",
                "user",
                name,
                extras={"limit": 100, "period": period},
            )

            data: Dict[Any, Any] = results["topalbums"].get("album")

            if data == [] or data is None:
                raise BlankException("No tracks found for this user.")

            if len(data) < 9:
                raise BlankException(
                    "Not enough albums to make a chart, sorry. Maybe try a different time period?"
                )

            image_data: List[Tuple[BytesIO, str]] = []

            total = 0
            chart_nsfw = False
            for track in data:
                if total == 9:
                    break
                try:
                    query = f"{track['name']} {track['artist']['name']}"
                    url, nsfw = await get_sp_cover(self.bot, query)

                    cover = await to_bytesio(ctx.session, url)
                    image_data.append((cover, shorten(track["name"], 15)))

                    if nsfw:
                        chart_nsfw = True

                    total += 1
                except (IndexError, NoCover):
                    continue

            image = await self.make_chart(image_data, name)

            await ctx.send(
                f"Top {lastfm_period[period]} albums chart for {name}",
                file=discord.File(image, filename="chart.png", spoiler=chart_nsfw),
            )
