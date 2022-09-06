import datetime
import re
import textwrap
from typing import Any, Dict, List, Literal, Optional, Union

import discord
from bot import Context
from discord.ext import commands
from utils import response_checker, human_join

from ._base import CogBase

anime_query_data = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    title {
      english
      romaji
    }
    coverImage {
      extraLarge
      color
    }
    description(asHtml: false)
    genres
    averageScore
    episodes
    status
    bannerImage
    siteUrl
    startDate {
      year
      month
      day
    }
    endDate {
      year
      month
      day
    }
    nextAiringEpisode {
      episode
      airingAt
    }
    source
  }
}
"""

manga_query_data = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
    title {
      english
      romaji
    }
    coverImage {
      extraLarge
      color
    }
    description(asHtml: false)
    genres
    averageScore
    status
    bannerImage
    siteUrl
    popularity
    startDate {
      year
      month
      day
    }
    endDate {
      year
      month
      day
    }
    source
    chapters
    volumes
    format
  }
}
"""


class AnimeCommands(CogBase):
    async def make_request(
        self, query: str, mode: Union[Literal["anime"], Literal["manga"]]
    ) -> Dict[Any, Any]:

        search_query = anime_query_data if mode == "anime" else manga_query_data

        data = {"query": search_query, "variables": {"search": query}}
        async with self.bot.session.post("https://graphql.anilist.co", json=data) as r:
            response_checker(r)
            results: Dict = await r.json()

            if results.get("errors"):
                raise ValueError(
                    textwrap.dedent(
                        f"""Unable to get info 
                        Error: {results['errors']['message']}
                        Status: {results['errors']['status']}
                        Location: Line `{results['errors']['locations']['line']}`, Column `{results['errors']['locations']['column']}`"""
                    )
                )
            return results["data"]["Media"]

    @commands.command(name="anime")
    async def anime(self, ctx: Context, *, query: str):

        data = await self.make_request(query, "anime")

        title: Dict | None = data.get("title")
        if title is None:
            anime_title = query
        else:
            anime_title = (
                title.get("romaji")
                if title.get("english") is None
                else title.get("english")
            )

        embed = discord.Embed(
            description=textwrap.shorten(
                re.sub(r"<br>", "", data["description"]), width=250
            ),
            title=str(anime_title).title(),
            url=data.get("siteUrl"),
        )

        files = []
        cover_image: Dict | None = data.get("coverImage")
        if cover_image is None:
            color = self.bot.embedcolor
        else:
            color = (
                int(re.sub("#", "0x", cover_image["color"]), 0)
                if cover_image.get("color")
                else self.bot.embedcolor
            )
            img = cover_image.get("extraLarge")
            if img:
                fp = await ctx.to_bytesio(img)
                file = discord.File(fp=fp, filename="thumbnail.png")
                files.append(file)
                embed.set_thumbnail(url="attachment://thumbnail.png")

        embed.color = color
        embed.add_field(
            name="Genres", value=human_join(data["genres"], final="and"), inline=False
        )
        score = data.get("averageScore")
        score_text = "Not rated" if score is None else f"{score}/100"

        embed.add_field(name="Score", value=score_text, inline=True)
        embed.add_field(
            name="Status",
            value=re.sub("_", " ", data["status"]).capitalize(),
            inline=True,
        )
        episodes: int | None = data.get("episodes")

        if episodes is None:
            airing: Dict | None = data.get("nextAiringEpisode")
            episodes = airing["episode"] - 1 if airing else None

        embed.add_field(
            name="Episodes",
            value=f"{episodes:,}" if episodes else "No episodes yet",
            inline=True,
        )

        banner: str | None = data.get("bannerImage")
        if banner:
            fp = await ctx.to_bytesio(banner)
            file2 = discord.File(fp=fp, filename="image.png")
            files.append(file2)
            embed.set_image(url="attachment://image.png")

        start: Dict | None = data.get("startDate")
        if start:
            embed.add_field(
                name="Started", value=f"{start['month']}-{start['day']}-{start['year']}"
            )

        end: Dict | None = data.get("endDate")
        if end:
            check = (
                False
                if any([end["month"] is None, end["day"] is None, end["year"] is None])
                else True
            )
            if check:
                embed.add_field(
                    name="Ended", value=f"{end['month']}-{end['day']}-{end['year']}"
                )
        embed.add_field(name="Source", value=str(data["source"]).title())

        await ctx.send(embed=embed, files=files)

    @commands.command(name="manga")
    async def manga(self, ctx: Context, *, query: str):

        data = await self.make_request(query, "manga")

        title: Dict | None = data.get("title")
        if title is None:
            anime_title = query
        else:
            anime_title = (
                title.get("romaji")
                if title.get("english") is None
                else title.get("english")
            )

        embed = discord.Embed(
            description=textwrap.shorten(
                re.sub(r"<br>", "", data["description"]), width=250
            ),
            title=str(anime_title).title(),
            url=data.get("siteUrl"),
        )
        files = []

        cover_image: Dict | None = data.get("coverImage")
        if cover_image is None:
            color = self.bot.embedcolor
        else:
            color = (
                int(re.sub("#", "0x", cover_image["color"]), 0)
                if cover_image.get("color")
                else self.bot.embedcolor
            )
            img = cover_image.get("extraLarge")
            if img:
                fp = await ctx.to_bytesio(img)
                file = discord.File(fp=fp, filename="thumbnail.png")
                files.append(file)
                embed.set_thumbnail(url="attachment://thumbnail.png")

        embed.color = color
        embed.add_field(
            name="Genres", value=human_join(data["genres"], final="and"), inline=False
        )
        score = data.get("averageScore")
        score_text = "Not rated" if score is None else f"{score}/100"

        embed.add_field(name="Score", value=score_text, inline=True)
        embed.add_field(
            name="Status",
            value=re.sub("_", " ", data["status"]).capitalize(),
            inline=True,
        )
        start: Dict | None = data.get("startDate")
        if start:
            embed.add_field(
                name="Started", value=f"{start['month']}-{start['day']}-{start['year']}"
            )

        end: Dict | None = data.get("endDate")
        if end:
            check = (
                False
                if any([end["month"] is None, end["day"] is None, end["year"] is None])
                else True
            )
            if check:
                embed.add_field(
                    name="Ended", value=f"{end['month']}-{end['day']}-{end['year']}"
                )

        banner: str | None = data.get("bannerImage")
        if banner:
            fp = await ctx.to_bytesio(banner)
            file2 = discord.File(fp=fp, filename="image.png")
            files.append(file2)
            embed.set_image(url="attachment://image.png")

        chapters: int | None = data.get("chapters")
        if chapters:
            embed.add_field(name="Chapters", value=f"{chapters:,}", inline=True)

        volumes: int | None = data.get("volumes")
        if chapters:
            embed.add_field(name="Volumes", value=f"{volumes:,}", inline=True)

        embed.add_field(name="Source", value=str(data["source"]).title())

        await ctx.send(embed=embed, files=files)
