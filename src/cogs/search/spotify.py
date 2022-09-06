import base64
from typing import Any, Dict

import discord
from bot import Bot, Context
from discord.ext import commands, tasks

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

    @commands.command(name="spotify", aliases=("s",))
    @commands.before_invoke(set_key)
    async def spotify(self, ctx: Context, *, query: str):
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bot.spotify_key}",
        }

        data = {"q": query, "type": "track", "market": "ES", "limit": "1"}

        await ctx.trigger_typing()

        async with self.bot.session.get(url, headers=headers, params=data) as r:
            results = await r.json()
            if results["tracks"]["items"] == []:
                raise ValueError("Couldn't find any tracks.")

        await ctx.send(results["tracks"]["items"][0]["external_urls"]["spotify"])
