from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any, Dict, Optional

import discord
from discord.ext import commands

from utils import (
    BaseCog,
    LastfmConverter,
    LastfmTimeConverter,
    NoCover,
    format_bytes,
    get_lastfm,
    get_sp_cover,
    lastfm_period,
    shorten,
    to_bytes,
    to_bytesio,
)

from .functions import *

if TYPE_CHECKING:
    from cogs.context import Context


class Chart(BaseCog):
    @commands.group(name="chart", aliases=("c",), invoke_without_command=True)
    async def chart(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(converter=LastfmTimeConverter, default="7day"),
    ):
        """View your top albums"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        async with ctx.typing():
            albums = await get_top_albums(self.bot, period, name)

            image_data: List[Tuple[BytesIO, str]] = []

            total = 0
            chart_nsfw = False
            for album in albums:
                if total == 9:
                    break
                try:
                    query = f"{album['name']} {album['artist']['name']}"
                    url, nsfw = await get_sp_cover(self.bot, query)

                    cover = await to_bytesio(ctx.session, url)
                    image_data.append((cover, shorten(album["name"], 15, ending="...")))

                    if nsfw:
                        chart_nsfw = True

                    total += 1
                except (IndexError, NoCover):
                    continue

            image = await make_chart(image_data, name)
            file = discord.File(image, filename="chart.png", spoiler=chart_nsfw)
            text = f"Top {lastfm_period[period]} albums chart for {name}"

            # if random.randint(1, 10) == 5:
            #    text += "\nWant a different chart? Try chart classic or chart advanced!"

            await ctx.send(text, file=file)

    @chart.command(name="classic", aliases=("c",), invoke_without_command=True)
    async def classic_chart(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(converter=LastfmTimeConverter, default="7day"),
    ):
        """View your top albums"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        async with ctx.typing():
            albums = await get_top_albums(self.bot, period, name)

            urls: List[str] = []

            total = 0
            chart_nsfw = False
            for album in albums:
                if total == 9:
                    break
                try:
                    query = f"{album['name']} {album['artist']['name']}"
                    url, nsfw = await get_sp_cover(self.bot, query)

                    urls.append(url)

                    if nsfw:
                        chart_nsfw = True

                    total += 1
                except (IndexError, NoCover):
                    continue

            images: List[bytes] = await asyncio.gather(
                *[to_bytes(ctx.session, url) for url in urls]
            )
            image = await format_bytes(ctx.guild.filesize_limit, images)
            file = discord.File(image, filename="chart.png", spoiler=chart_nsfw)

            await ctx.send(
                f"Top {lastfm_period[period]} albums chart for {name}", file=file
            )

    @chart.command(name="advanced", aliases=("a",), invoke_without_command=True)
    async def advanced_chart(
        self,
        ctx: Context,
        username: Optional[LastfmConverter] = commands.Author,
        period: str = commands.parameter(converter=LastfmTimeConverter, default="7day"),
    ):
        """View your top albums

        THIS COMMAND IS NOT FINISHED!!!"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        async with ctx.typing():
            albums = await get_top_albums(self.bot, period, name)

            image_data: List[Tuple[BytesIO, str]] = []

            chart_nsfw = False
            for index, album in enumerate(albums):
                if index == 50:
                    break

                try:
                    query = f"{album['name']} {album['artist']['name']}"
                    url, nsfw = await get_sp_cover(self.bot, query)

                    image = await to_bytesio(ctx.session, url)
                    image_data.append(
                        (image, f"{album['artist']['name']} - {album['name']}")
                    )

                    if nsfw:
                        chart_nsfw = True

                except (IndexError, NoCover):
                    continue

            image = await make_advanced_chart(image_data)

            file = discord.File(image, filename="chart.png", spoiler=chart_nsfw)

        await ctx.send(
            f"Top 50 {lastfm_period[period]} albums chart for {name}\nThis command is not finished yet, work in progress.",
            file=file,
        )
