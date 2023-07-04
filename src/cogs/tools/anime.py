from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING, Any, Dict, Literal, Union

import discord
from discord.ext import commands

from utils import anime_query_data, human_join, response_checker, BaseCog

if TYPE_CHECKING:
    from cogs.context import Context


class AnimeCommands(BaseCog):
    async def make_request(
        self, query: str, mode: Union[Literal["anime"], Literal["manga"]]
    ) -> Dict[Any, Any]:
        search_query = (
            anime_query_data
            if mode == "anime"
            else re.sub("ANIME", "MANGA", anime_query_data)
        )

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

    def cleanup_html(self, text: str) -> str:
        text = re.sub("<br>", "\n", text)
        text = re.sub("<b>", "**", text)
        text = re.sub("</b>", "**", text)

        return text

    def basic_setup(self, data: Dict, query: str) -> discord.Embed:
        try:
            title = f"{data['title']['english']}/{data['title']['native']}"
        except KeyError:
            title = query

        embed = discord.Embed(
            title=title,
            url=data.get("siteUrl"),
            description=self.cleanup_html(
                textwrap.shorten(data["description"], width=250)
            ),
        )

        cover: Dict | None = data.get("coverImage")
        if cover:
            embed.set_thumbnail(url=cover.get("extraLarge"))
            embed.color = (
                discord.Color.from_str(cover["color"])
                if cover.get("color")
                else self.bot.embedcolor
            )

        embed.set_image(url=data.get("bannerImage"))
        embed.add_field(
            name="Genres", value=human_join(data["genres"], final="and"), inline=False
        )
        embed.add_field(
            name="Tags",
            value=human_join([tag["name"] for tag in data["tags"]][:5], final="and")
            if data["tags"]
            else "No tags yet",
            inline=False,
        )
        embed.add_field(
            name="Score",
            value=f"{data['averageScore']}/100"
            if data.get("averageScore")
            else "Not yet rated",
        )
        embed.add_field(
            name="Status", value=re.sub("_", " ", data["status"]).capitalize()
        )
        if type == "anime":
            episode_text = ""
            if data.get("episodes"):
                episode_text = f"{data['episodes']:,}"
            else:
                next_airing: Dict | None = data.get("nextAiringEpisode")
                episode_text = (
                    f"{next_airing['episode']-1:,} \nNext episode <t:{next_airing['airingAt']}:R>"
                    if next_airing
                    else "No episodes yet"
                )
            embed.add_field(name="Episodes", value=episode_text)

        volumes: int | None = data.get("volumes")
        if volumes:
            embed.add_field(name="Volumes", value=f"{volumes:,}")

        chapters: int | None = data.get("chapters")
        if chapters:
            embed.add_field(name="Chapters", value=f"{chapters:,}")

        trailer_format = {
            "youtube": "https://www.youtube.com/watch?v=",
            "dailymotion": "https://www.dailymotion.com/video/",
        }
        trailer = data.get("trailer")
        if trailer:
            site = trailer_format[trailer["site"]]
            embed.add_field(
                name="Trailer", value=f"[Watch here]({site}{trailer['id']})"
            )

        embed.set_footer(text=f"Source: {str(data['source']).capitalize()}")

        return embed

    @commands.command(name="anime")
    async def anime(self, ctx: Context, *, query: str):
        """Search for an anime"""
        data = await self.make_request(query, "anime")
        embed = self.basic_setup(data, query)
        await ctx.send(embed=embed)

    @commands.command(name="manga")
    async def manga(self, ctx: Context, *, query: str):
        """Search for a manga"""
        data = await self.make_request(query, "manga")
        embed = self.basic_setup(data, query)
        await ctx.send(embed=embed)
