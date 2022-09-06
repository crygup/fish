from __future__ import annotations
import textwrap

from typing import Any, Dict, List, Literal, Tuple
from typing import Literal as L
from typing import Union

import discord
from bot import Context
from discord.ext import commands
from utils import response_checker, AuthorView

from ._base import CogBase

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


class YoutubeCommands(CogBase):
    async def search_method(
        self,
        ctx: Context,
        query: str,
        type: str,
    ):
        url = f"https://www.googleapis.com/youtube/v3/search"
        params = {
            "q": query,
            "key": self.bot.config["keys"]["google-search"],
            "part": "snippet",
            "type": type,
            "maxResults": 25,
        }

        await ctx.trigger_typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()

            url = f"https://www.youtube.com/{link_converter[type]}{data['items'][0]['id'][id_converter[type]]}"

        videos = data["items"]
        view = YoutubeView(ctx, videos, type)
        await ctx.send(url, view=view)

    @commands.group(name="youtube", aliases=("yt",), invoke_without_command=True)
    async def youtube(self, ctx: Context, *, query: str):
        await self.search_method(ctx, query, "video")

    @youtube.group(name="channel", aliases=("ch",))
    async def youtube_channel(self, ctx: Context, *, query: str):
        await self.search_method(ctx, query, "channel")

    @youtube.group(name="playlist", aliases=("pl",))
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
                    emoji="<:yt:1016728285905965056>",
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
        super().__init__(ctx)
        self.add_item(YoutubeDropdown(videos, type))
