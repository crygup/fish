from __future__ import annotations

import asyncio
import re
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Tuple, Union

from discord.ext import commands

from .functions import response_checker
from .vars import SpotifySearchData

if TYPE_CHECKING:
    from extensions.context import Context

SVG_URL = (
    "https://raw.githubusercontent.com/twitter/twemoji/master/assets/svg/{chars}.svg"
)


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


async def render_with_rsvg(blob):
    rsvg = "rsvg-convert --width=1024"
    proc = await asyncio.create_subprocess_shell(
        rsvg,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(blob)
    return BytesIO(stdout), stderr


class TwemojiConverter(commands.Converter):
    """Converts str to twemoji bytesio"""

    async def convert(self, ctx: Context, argument: str) -> BytesIO:
        if len(argument) >= 8:
            raise commands.BadArgument("Too long to be an emoji")

        VS_16 = "\N{VARIATION SELECTOR-16}"

        resp = None
        blob = b""
        while not resp or resp.status != 200:
            chars = "-".join(f"{ord(c):x}" for c in argument)
            async with ctx.bot.session.get(SVG_URL.format(chars=chars)) as resp:
                if resp.status != 200:
                    if VS_16 in argument:
                        new_ipt = argument.removeprefix(VS_16)
                        if new_ipt == argument:
                            new_ipt = argument.replace(VS_16, "")
                        ipt = new_ipt
                        continue
                    raise commands.BadArgument("Not a valid unicode emoji.")
                blob = await resp.read()

        converted, stderr = await render_with_rsvg(blob)

        if stderr:
            raise Exception(stderr.decode())

        return converted
