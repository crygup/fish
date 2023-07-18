from __future__ import annotations

import random
import textwrap
from typing import TYPE_CHECKING, Any, Dict, List

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import (
    AuthorView,
    GoogleImageData,
    GoogleImagePageSource,
    Pager,
    response_checker,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

id_converter = {
    "video": "videoId",
    "channel": "channelId",
    "playlist": "playlistId",
}

link_converter = {
    "video": "watch?v=",
    "channel": "channel/",
    "playlist": "playlist?list=",
}


class Google(Cog):
    def __init__(self, bot: Fishie) -> None:
        self.bot = bot

    @commands.hybrid_command(name="google")
    async def google(self, ctx: Context, *, query: str):
        """Search something on the web"""

        url = f"https://customsearch.googleapis.com/customsearch/v1"
        params = {
            "cx": self.bot.config["keys"]["google_id"],
            "q": query,
            "key": random.choice(self.bot.config["keys"]["google"]),
            "safe": "off"
            if isinstance(
                ctx.channel,
                (discord.DMChannel, discord.PartialMessageable, discord.GroupChannel),
            )
            else ["active", "off"][ctx.channel.is_nsfw()],
        }
        await ctx.typing()

        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()

            embed = discord.Embed(color=discord.Colour.pink())
            embed.set_footer(
                text=f"About {data['searchInformation']['formattedTotalResults']} results ({data['searchInformation']['formattedSearchTime']} seconds)"
            )

            embed.title = f"Google Search - {query}"[:256]

            text = ""
            items = data["items"]

            added = 0
            for item in items:
                if added == 5:
                    break
                try:
                    text += f"[{item['title']}]({item['link']})\n{item['snippet']}\n\n"
                    added += 1
                except KeyError:
                    continue
            embed.description = text

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="image",
        aliases=("img", "i"),
        extras={"google-command": True},
    )
    async def google_image(self, ctx: Context, *, query: str):
        """Search google images"""
        url = f"https://customsearch.googleapis.com/customsearch/v1"
        params = {
            "cx": self.bot.config["keys"]["google_id"],
            "q": query,
            "key": random.choice(self.bot.config["keys"]["google"]),
            "searchType": "image",
            "safe": "off"
            if isinstance(
                ctx.channel,
                (discord.DMChannel, discord.PartialMessageable, discord.GroupChannel),
            )
            else ["active", "off"][ctx.channel.is_nsfw()],
        }

        await ctx.typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            results = await r.json()

            items = results.get("items")

            if items is None:
                raise commands.BadArgument("No search results found for this query.")

            entries = [
                GoogleImageData(
                    image_url=data["link"],
                    url=data["image"]["contextLink"],
                    snippet=data["snippet"],
                    query=query,
                    author=ctx.author,
                )
                for data in results["items"]
            ]

        pager = Pager(GoogleImagePageSource(entries), ctx=ctx)
        await pager.start(ctx)

    async def search_method(
        self,
        ctx: Context,
        query: str,
        type: str,
    ):
        url = f"https://www.googleapis.com/youtube/v3/search"
        params = {
            "q": query,
            "key": random.choice(self.bot.config["keys"]["google"]),
            "part": "snippet",
            "type": type,
            "maxResults": 25,
        }

        await ctx.typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()
            try:
                url = f"https://www.youtube.com/{link_converter[type]}{data['items'][0]['id'][id_converter[type]]}"
            except (IndexError, KeyError):
                raise commands.BadArgument("Couldn't find any results.")

        videos = data["items"]
        view = YoutubeView(ctx, videos, type)
        await ctx.send(url, view=view)

    @commands.hybrid_group(
        name="youtube",
        aliases=("yt",),
        invoke_without_command=True,
        fallback="video",
        extras={"google-command": True},
    )
    @app_commands.describe(query="Video to search for")
    async def youtube(self, ctx: Context, *, query: str):
        """Look up something on youtube"""
        await self.search_method(ctx, query, "video")

    @youtube.command(
        name="channel", aliases=("ch",), description="Search for a playlist"
    )
    @app_commands.describe(query="Channel to search for")
    async def youtube_channel(self, ctx: Context, *, query: str):
        await self.search_method(ctx, query, "channel")

    @youtube.command(
        name="playlist", aliases=("pl",), description="Search for a playlist"
    )
    @app_commands.describe(query="Playlist to search for")
    async def youtube_playlist(self, ctx: Context, *, query: str):
        await self.search_method(ctx, query, "playlist")


class YoutubeDropdown(discord.ui.Select):
    def __init__(self, videos: List[Dict[Any, Any]], type: str):
        self._type = type
        self.videos = videos

        start = 0
        options = []
        for vid in videos:
            options.append(
                discord.SelectOption(
                    label=textwrap.shorten(vid["snippet"]["title"], width=100),
                    value=str(start),
                    emoji="<:yt:1097399470842466334>",
                )
            )
            start += 1

        super().__init__(
            placeholder=textwrap.shorten(videos[0]["snippet"]["title"], width=100),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> Any:
        if interaction.message is None:
            await interaction.response.defer()
            return

        index = self.values[0]
        link = link_converter[self._type]
        data = self.videos[int(index)]
        url = f"https://www.youtube.com/{link}{data['id'][id_converter[self._type]]}"
        self.placeholder = data["snippet"]["title"]
        await interaction.message.edit(content=url, view=self.view)
        await interaction.response.defer()


class YoutubeView(AuthorView):
    def __init__(self, ctx: Context, videos: List[Dict[Any, Any]], type):
        super().__init__(ctx=ctx)
        self.add_item(YoutubeDropdown(videos, type))
