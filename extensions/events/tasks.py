from __future__ import annotations

import base64
import os
import re
import subprocess
from typing import TYPE_CHECKING, Any, Dict

from discord.ext import commands, tasks

from core import Cog
from utils import run


class Tasks(Cog):
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

    def delete_videos(self):
        valid_formats = (
            "mp4",
            "webm",
            "mov",
            "mp3",
            "ogg",
            "wav",
            "part",
            "ytdl",
        )
        for file in os.listdir(r"files/downloads"):
            if file.endswith(valid_formats):
                subprocess.run(
                    [f"rm {file}"], shell=True, cwd="files/downloads", check=False
                )

    @tasks.loop(minutes=30.0)
    async def set_key_task(self):
        await self.set_spotify_key()

    async def cog_unload(self):
        self.set_key_task.cancel()
        self.delete_videos_task.cancel()

    async def cog_load(self) -> None:
        self.set_key_task.start()
        self.delete_videos_task.start()

    @tasks.loop(minutes=10.0)
    async def delete_videos_task(self):
        self.delete_videos()
