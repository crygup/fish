from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import SpotifyConverter, get_sp_cover, to_bytesio, BaseCog

if TYPE_CHECKING:
    from cogs.context import Context


class SpotifyCommands(BaseCog):
    async def album_command(self, ctx: Context, query: Optional[str]):
        await ctx.trigger_typing()
        Spotify = SpotifyConverter(ctx, "album")
        search = await Spotify.get_query(query)

        await ctx.send(await Spotify.search_album(*search))

    async def artist_command(self, ctx: Context, query: Optional[str]):
        await ctx.trigger_typing()
        Spotify = SpotifyConverter(ctx, "artist")
        search = await Spotify.get_query(query)

        await ctx.send(await Spotify.search_artist(*search))

    @commands.hybrid_group(
        name="spotify",
        aliases=("sp", "s", "song", "track"),
        invoke_without_command=True,
        fallback="track",
    )
    @app_commands.describe(query="The name of the track")
    async def spotify(self, ctx: Context, *, query: Optional[str]):
        """Search for a track on spotify"""

        await ctx.trigger_typing()
        Spotify = SpotifyConverter(ctx, "track")
        search = await Spotify.get_query(query)

        await ctx.send(await Spotify.search_track(*search))

    @spotify.command(name="album", aliases=("ab",))
    @app_commands.describe(query="The name of the album")
    async def album_hybrid(self, ctx: Context, *, query: Optional[str]):
        """Search for an album on spotify"""

        await self.album_command(ctx, query)

    @spotify.command(name="artist", aliases=("art",))
    @app_commands.describe(query="The name of the artist")
    async def artist_hybrid(self, ctx: Context, *, query: Optional[str]):
        """Search for an artist on spotify"""

        await self.artist_command(ctx, query)

    @commands.hybrid_command(name="cover", aliases=("co",))
    @app_commands.describe(query="The name of the album")
    async def cover_hybrid(self, ctx: Context, *, query: Optional[str]):
        """Get the cover for an album on spotify"""

        await ctx.trigger_typing()
        Spotify = SpotifyConverter(ctx, "album")
        search = await Spotify.get_query(query)

        url, nsfw = await get_sp_cover(self.bot, search[0])
        fp = await to_bytesio(ctx.session, url)
        await ctx.send(file=discord.File(fp=fp, filename="cover.png", spoiler=nsfw))

    @commands.command(name="album", aliases=("ab",))
    async def album(self, ctx: Context, *, query: Optional[str]):
        """Search for an album on spotify"""

        await self.album_command(ctx, query)

    @commands.command(name="artist", aliases=("art",))
    async def artist(self, ctx: Context, *, query: Optional[str]):
        """Search for an artist on spotify"""

        await self.artist_command(ctx, query)
