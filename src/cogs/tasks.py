from __future__ import annotations

import base64
import os
import re
from typing import TYPE_CHECKING, Any, Dict
import discord

from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot import Bot

from utils import DevError


async def setup(bot: Bot):
    await bot.add_cog(Tasks(bot))


class Tasks(commands.Cog, name="tasks"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.tatsu_reps = [663851322591936514, 121738169900204034, 757872553799843852]

    async def cog_unload(self):
        self.set_key_task.cancel()
        self.tatsu_reminders.cancel()

    async def cog_load(self) -> None:
        self.set_key_task.start()
        self.tatsu_reminders.start()

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

            raise DevError("Unable to set spotify key.")

    @tasks.loop(minutes=30.0)
    async def set_key_task(self):
        await self.set_spotify_key()

    @tasks.loop(minutes=10.0)
    async def delete_videos(self):
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
        for file in os.listdir("src/files/videos"):
            if file.endswith(valid_formats):
                if re.sub("(ytdl|part)", "", file) not in self.bot.current_downloads:
                    os.remove(f"src/files/videos/{file}")

    @tasks.loop(minutes=1)
    async def tatsu_reminders(self):
        await self.bot.wait_until_ready()
        now = discord.utils.utcnow()
        replacement = now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_rep = replacement.replace(day=now.day)

        if now >= next_rep:
            return

        for user_id in self.tatsu_reps:
            user = self.bot.get_user(user_id)

            if user is None:
                continue

            try:
                await user.send("you can rep liz again\n t!rep 766953372309127168")
            except:
                ch: discord.TextChannel = self.bot.get_channel(909210055579152404)  # type: ignore
                if ch is None:
                    continue
                await ch.send(
                    f"you can rep liz again {user.mention}\n t!rep 766953372309127168"
                )
