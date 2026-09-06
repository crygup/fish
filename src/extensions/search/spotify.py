from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import get_sp_cover, response_checker, spotify, to_image

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


DISCORD_USER_RE = re.compile(r"^(?:<@!?(?P<mention>\d{15,22})>|(?P<id>\d{15,22}))$")


def _spotify_target_id(query: str | None) -> int | None:
    if query is None:
        return None
    match = DISCORD_USER_RE.fullmatch(query.strip())
    if not match:
        return None
    return int(match.group("mention") or match.group("id"))


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

    async def _spotify_query(
        self,
        ctx: Context,
        mode: Literal["track", "album", "artist"],
        query: str | None,
    ) -> str:
        raw = query.strip() if query else ""
        target_id = _spotify_target_id(raw) if raw else ctx.author.id
        if raw and target_id is None:
            return raw

        username = self.bot.db_cache.lastfm.get(target_id or ctx.author.id)
        if not username:
            subject = "This user" if raw else "You"
            raise commands.BadArgument(
                f"{subject} must connect a Last.fm account with `fish accounts` "
                "to search without a query."
            )
        response = await self.bot.lfm_get(
            {"method": "user.getrecenttracks", "user": username, "limit": 1}
        )
        tracks = response.get("recenttracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not tracks or not isinstance(tracks[0], dict):
            raise commands.BadArgument(
                f"No recent tracks were found for **{username}**."
            )
        track = tracks[0]
        artist_data = track.get("artist")
        artist = (
            str(artist_data.get("#text") or artist_data.get("name") or "")
            if isinstance(artist_data, dict)
            else str(artist_data or "")
        ).strip()
        if mode == "artist":
            if not artist:
                raise commands.BadArgument("Last.fm did not provide a usable artist.")
            return artist
        if mode == "album":
            album_data = track.get("album")
            album = (
                str(album_data.get("#text") or album_data.get("title") or "")
                if isinstance(album_data, dict)
                else str(album_data or "")
            ).strip()
            if not album:
                raise commands.BadArgument(
                    "Last.fm did not provide an album for that user's current track."
                )
            return f'album:"{album}" artist:"{artist}"'
        title = str(track.get("name") or "").strip()
        if not title:
            raise commands.BadArgument("Last.fm did not provide a usable track.")
        return f'track:"{title}" artist:"{artist}"'

    @commands.hybrid_group(
        name="spotify",
        fallback="track",
        aliases=("sp", "s", "song", "sptrack"),
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="Track name or Discord user. Defaults to your current Last.fm track."
    )
    async def spotify(self, ctx: Context, *, query: str | None = None):
        """Search for a track on Spotify."""
        async with ctx.typing():
            resolved = await self._spotify_query(ctx, "track", query)
            await ctx.send(await self.search(ctx=ctx, mode="track", query=resolved))

    @spotify.command(name="album", aliases=("ab",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="Album name or Discord user. Defaults to your current Last.fm album."
    )
    async def spotify_album(self, ctx: Context, *, query: str | None = None):
        """Search for an album on Spotify."""
        async with ctx.typing():
            resolved = await self._spotify_query(ctx, "album", query)
            await ctx.send(await self.search(ctx=ctx, mode="album", query=resolved))

    @spotify.command(name="artist", aliases=("art",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="Artist name or Discord user. Defaults to your current Last.fm artist."
    )
    async def spotify_artist(self, ctx: Context, *, query: str | None = None):
        """Search for an artist on Spotify."""
        async with ctx.typing():
            resolved = await self._spotify_query(ctx, "artist", query)
            await ctx.send(await self.search(ctx=ctx, mode="artist", query=resolved))

    @commands.command(name="spalbum", aliases=("ab",))
    async def spalbums_text(self, ctx: Context, *, query: str | None = None):
        """Search for an album on Spotify using a text command."""
        async with ctx.typing():
            resolved = await self._spotify_query(ctx, "album", query)
            await ctx.send(await self.search(ctx=ctx, mode="album", query=resolved))

    @commands.command(name="spartist", aliases=("art",))
    async def spartists_text(self, ctx: Context, *, query: str | None = None):
        """Search for an artist on Spotify using a text command."""
        async with ctx.typing():
            resolved = await self._spotify_query(ctx, "artist", query)
            await ctx.send(await self.search(ctx=ctx, mode="artist", query=resolved))

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
