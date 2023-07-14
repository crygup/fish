from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Tuple, Union

from discord.ext import commands

from .functions import response_checker
from .vars import SpotifySearchData

if TYPE_CHECKING:
    from extensions.context import Context


class URLConverter(commands.Converter[str]):
    async def convert(self, ctx: Context, argument: str) -> str:
        if not re.match(r"^https?://", argument):
            argument = f"http://{argument}"

        return argument


class SpotifyConverter:
    format_mode = {
        "track": "tracks",
        "album": "albums",
        "artist": "artists",
        "track,album,artist": "albums",
    }

    def __init__(
        self,
        ctx: Context,
        mode: Union[
            Literal["track"], Literal["album"], Literal["artist"], Literal["all"]
        ],
    ):
        super().__init__()
        self.mode = mode if mode != "all" else "track,album,artist"
        self.ctx = ctx

    async def search_raw(self, query: str) -> Dict[Any, Any]:
        ctx = self.ctx
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.bot.spotify_key}",
        }

        api_data = {"q": query, "type": self.mode, "limit": "10", "market": "US"}

        async with ctx.session.get(url, headers=headers, params=api_data) as resp:
            response_checker(resp)
            data: Optional[Dict[Any, Any]] = (
                (await resp.json()).get(self.format_mode[self.mode]).get(f"items")
            )

        if data == [] or data is None:
            raise commands.BadArgument("No info found for this query")

        return data

    async def search_album(self, query: str) -> str:
        data = await self.search_raw(query)

        return data[0]["external_urls"]["spotify"]

    async def search_artist(self, query: str) -> str:
        data = await self.search_raw(query)

        return data[0]["external_urls"]["spotify"]

    async def search_track(self, query: str) -> str:
        data = await self.search_raw(query)

        return data[0]["external_urls"]["spotify"]
