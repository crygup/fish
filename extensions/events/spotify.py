from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, Dict

from discord.ext import commands, tasks

from core import Cog

if TYPE_CHECKING:
    from core import Fishie


class Spotify(Cog):
    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    async def cog_unload(self):
        self.set_key_task.cancel()

    async def cog_load(self) -> None:
        self.set_key_task.start()

    async def set_spotify_key(self):
        url = "https://accounts.spotify.com/api/token"
        sid = self.bot.config["keys"]["spotify_id"]
        ss = self.bot.config["keys"]["spotify_secret"]

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

            raise commands.BadArgument("Unable to set spotify key.")

    @tasks.loop(minutes=30.0)
    async def set_key_task(self):
        await self.set_spotify_key()
