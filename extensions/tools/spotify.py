from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import get_sp_cover, response_checker, to_image

if TYPE_CHECKING:
    from extensions.context import Context


class Spotify(Cog):
    format_mode = {
        "track": "tracks",
        "album": "albums",
        "artist": "artists",
        "track,album,artist": "albums",
    }

    async def search(
        self,
        ctx: Context,
        mode: Union[
            Literal["track"], Literal["album"], Literal["artist"], Literal["all"]
        ],
        query: str,
    ) -> str:
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.bot.spotify_key}",
        }

        api_data = {"q": query, "type": mode, "limit": "10", "market": "US"}

        async with ctx.session.get(url, headers=headers, params=api_data) as resp:
            response_checker(resp)
            data: Optional[Dict[Any, Any]] = (
                (await resp.json()).get(self.format_mode[mode]).get(f"items")
            )

        if data == [] or data is None:
            raise commands.BadArgument("No info found for this query")

        return data[0]["external_urls"]["spotify"]

    @commands.hybrid_group(
        name="spotify",
        aliases=("sp", "s", "song", "track"),
        invoke_without_command=True,
        fallback="track",
    )
    @app_commands.describe(query="The name of the track")
    async def spotify(self, ctx: Context, *, query: str):
        """Search for a track on spotify"""

        await ctx.typing()

        await ctx.send(await self.search(ctx=ctx, mode="track", query=query))

    @spotify.command(name="album", aliases=("ab",))
    @app_commands.describe(query="The name of the album")
    async def album_hybrid(self, ctx: Context, *, query: str):
        """Search for an album on spotify"""

        await ctx.send(await self.search(ctx=ctx, mode="artist", query=query))

    @spotify.command(name="artist", aliases=("art",))
    @app_commands.describe(query="The name of the artist")
    async def artist_hybrid(self, ctx: Context, *, query: str):
        """Search for an artist on spotify"""

        await ctx.send(await self.search(ctx=ctx, mode="artist", query=query))

    @commands.hybrid_command(name="cover", aliases=("co",))
    @app_commands.describe(query="The name of the album")
    async def cover_hybrid(self, ctx: Context, *, query: str):
        """Get the cover for an album on spotify"""

        await ctx.typing()

        url, nsfw = await get_sp_cover(self.bot, query)
        fp = await to_image(ctx.session, url)
        await ctx.send(file=discord.File(fp=fp, filename="cover.png", spoiler=nsfw))

    @commands.command(name="album", aliases=("ab",))
    async def album(self, ctx: Context, *, query: str):
        """Search for an album on spotify"""

        await ctx.send(await self.search(ctx=ctx, mode="album", query=query))

    @commands.command(name="artist", aliases=("art",))
    async def artist(self, ctx: Context, *, query: str):
        """Search for an artist on spotify"""

        await ctx.send(await self.search(ctx=ctx, mode="artist", query=query))
