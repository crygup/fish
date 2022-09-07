import base64
from typing import Any, Dict
from typing import Literal as L
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot import Context
from utils import get_lastfm, get_lastfm_data, get_sp_cover, to_bytesio

from ._base import CogBase


class SpotifyCommands(CogBase):
    async def cog_unload(self):
        self.set_key_task.cancel()

    async def cog_load(self) -> None:
        self.set_key_task.start()

    async def set_spotify_key(self):
        url = "https://accounts.spotify.com/api/token"
        sid = self.bot.config["keys"]["spotify-id"]
        ss = self.bot.config["keys"]["spotify-secret"]
        encoded_key = base64.b64encode(f"{sid}:{ss}".encode("ascii")).decode("ascii")

        headers = {
            "Authorization": f"Basic {encoded_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "client_credentials",
        }

        async with self.bot.session.post(url, headers=headers, data=data) as r:
            results: Dict[Any, Any] = await r.json()
            if results.get("access_token"):
                self.bot.spotify_key = results["access_token"]
                return

            raise ValueError("Unable to set spotify key.")

    @tasks.loop(minutes=30.0)
    async def set_key_task(self):
        await self.set_spotify_key()

    async def set_key(self, _):
        key = self.bot.spotify_key
        if key is None:
            await self.set_spotify_key()

    async def get_query(self, ctx: Context, query: Optional[str], mode: str) -> str:
        if not query:
            name = await get_lastfm(self.bot, ctx.author.id)

            info = await get_lastfm_data(
                self.bot, "2.0", "user.getrecenttracks", "user", name
            )

            if info["recenttracks"]["track"] == []:
                raise ValueError("No recent tracks found for this user.")

            track = info["recenttracks"]["track"][0]
            begin = {
                "track": track["name"],
                "album": track["album"]["#text"],
                "artist": None,
            }
            return f"{begin[mode]} artist:{track['artist']['#text']}"
        else:
            return query

    async def get_spotify_search_data(
        self,
        query: str,
        mode: Union[L["track"], L["album"], L["artist"]],
    ) -> Dict:
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bot.spotify_key}",
        }

        data = {"q": query, "type": mode, "market": "ES", "limit": "1"}

        async with self.bot.session.get(url, headers=headers, params=data) as r:
            results = await r.json()

        if results[f"{mode}s"]["items"] == []:
            raise ValueError("No info found for this query")

        return results[f"{mode}s"]["items"][0]

    @commands.hybrid_group(
        name="spotify",
        aliases=("sp", "s"),
        invoke_without_command=True,
        fallback="track",
    )
    @app_commands.describe(query="The name of the track")
    async def spotify(self, ctx: Context, query: Optional[str]):
        """Search for a track on spotify"""
        await ctx.trigger_typing()
        to_search = await self.get_query(ctx, query, "track")

        data = await self.get_spotify_search_data(to_search, "track")
        await ctx.send(data["external_urls"]["spotify"])

    @spotify.command(name="album", aliases=("ab",))
    @app_commands.describe(query="The name of the album")
    async def album(self, ctx: Context, query: Optional[str]):
        """Search for an album on spotify"""
        await ctx.trigger_typing()
        to_search = await self.get_query(ctx, query, "album")

        data = await self.get_spotify_search_data(to_search, "album")
        await ctx.send(data["external_urls"]["spotify"])

    @spotify.command(name="artist", aliases=("art",))
    @app_commands.describe(query="The name of the artist")
    async def artist(self, ctx: Context, query: Optional[str]):
        """Search for an artist on spotify"""
        await ctx.trigger_typing()
        to_search = await self.get_query(ctx, query, "album")

        data = await self.get_spotify_search_data(to_search, "artist")
        await ctx.send(data["external_urls"]["spotify"])

    @commands.hybrid_command(name="cover", aliases=("co",))
    @app_commands.describe(query="The name of the album")
    async def cover(self, ctx: Context, query: Optional[str]):
        """Get the cover for an album on spotify"""
        await ctx.trigger_typing()
        to_search = await self.get_query(ctx, query, "album")

        url, nsfw = await get_sp_cover(self.bot, to_search)
        fp = await to_bytesio(ctx.session, url)
        file = discord.File(fp=fp, filename="cover.png", spoiler=nsfw)
        await ctx.send(file=file)
