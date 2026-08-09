from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import get_sp_cover, response_checker, spotify, to_image

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Spotify(Cog):
    """Public Spotify catalog lookups."""

    emoji = spotify

    format_mode = {
        "track": "tracks",
        "album": "albums",
        "artist": "artists",
        "track,album,artist": "albums",
    }

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    async def search(
        self,
        ctx: Context,
        mode: Union[
            Literal["track"], Literal["album"], Literal["artist"], Literal["all"]
        ],
        query: str,
    ) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.bot.spotify_key}",
        }
        api_data = {"q": query, "type": mode, "limit": "10", "market": "US"}
        async with ctx.session.get(
            "https://api.spotify.com/v1/search",
            headers=headers,
            params=api_data,
        ) as response:
            response_checker(response)
            data: Optional[Dict[Any, Any]] = (
                (await response.json()).get(self.format_mode[mode]).get("items")
            )
        if not data:
            raise commands.BadArgument("No info found for this query")
        return data[0]["external_urls"]["spotify"]

    @commands.hybrid_group(
        name="spotify",
        fallback="track",
        aliases=("sp", "s", "song", "sptrack"),
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the track")
    async def spotify(self, ctx: Context, *, query: str):
        """Search for a track on Spotify."""
        async with ctx.typing():
            await ctx.send(await self.search(ctx=ctx, mode="track", query=query))

    @spotify.command(name="album", aliases=("ab",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the album")
    async def spotify_album(self, ctx: Context, *, query: str):
        """Search for an album on Spotify."""
        async with ctx.typing():
            await ctx.send(await self.search(ctx=ctx, mode="album", query=query))

    @spotify.command(name="artist", aliases=("art",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the artist")
    async def spotify_artist(self, ctx: Context, *, query: str):
        """Search for an artist on Spotify."""
        async with ctx.typing():
            await ctx.send(await self.search(ctx=ctx, mode="artist", query=query))

    @commands.command(name="spalbum", aliases=("ab",))
    async def spalbums_text(self, ctx: Context, *, query: str):
        """Search for an album on Spotify using a text command."""
        async with ctx.typing():
            await ctx.send(await self.search(ctx=ctx, mode="album", query=query))

    @commands.command(name="spartist", aliases=("art",))
    async def spartists_text(self, ctx: Context, *, query: str):
        """Search for an artist on Spotify using a text command."""
        async with ctx.typing():
            await ctx.send(await self.search(ctx=ctx, mode="artist", query=query))

    @commands.hybrid_command(name="cover", aliases=("co",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="The name of the album")
    async def cover(self, ctx: Context, *, query: str):
        """Get the cover for an album on Spotify."""
        await ctx.typing()
        url, nsfw = await get_sp_cover(self.bot, query)
        fp = await to_image(ctx.session, url)
        await ctx.send(file=discord.File(fp=fp, filename="cover.png", spoiler=nsfw))


async def setup(bot: Fishie):
    await bot.add_cog(Spotify(bot))
