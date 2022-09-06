import base64
from typing import Any, Dict, Optional

import discord
from bot import Bot, Context
from discord.ext import commands, tasks
from utils import LastfmClient, get_lastfm

from ._base import CogBase


class SpotifyCommands(CogBase):
    def __init__(self, bot: Bot):
        self.bot = bot

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

    async def get_spotify_url(self, query: str) -> str:
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bot.spotify_key}",
        }

        data = {"q": query, "type": "track", "market": "ES", "limit": "1"}

        async with self.bot.session.get(url, headers=headers, params=data) as r:
            results = await r.json()
            if results["tracks"]["items"] == []:
                raise ValueError("Couldn't find any tracks.")

        return results["tracks"]["items"][0]["external_urls"]["spotify"]

    @commands.command(name="spotify", aliases=("s",))
    @commands.before_invoke(set_key)
    async def spotify(self, ctx: Context, *, query: str):
        await ctx.trigger_typing()

        url = await self.get_spotify_url(query)
        await ctx.send(url)

    @commands.command(name="cover", aliases=("co",))
    async def cover(self, ctx: Context, *, query: Optional[str]):
        """Gets the album cover for your recent track or query"""
        if not query:
            name = await get_lastfm(ctx.bot, ctx.author.id)
            info = await LastfmClient(
                self.bot, "2.0", "user.getrecenttracks", "user", name
            )

            if info["recenttracks"]["track"] == []:
                raise ValueError("No recent tracks found for this user.")
            track = info["recenttracks"]["track"][0]
            to_search = f"{track['name']} artist:{track['artist']['#text']}"
        else:
            to_search = query

        url = await self.get_spotify_url(to_search)
        await ctx.send(url)
