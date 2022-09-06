from typing import Dict, Optional, Union

import discord
from bot import Bot
from discord import app_commands
from discord.ext import commands
from utils import get_lastfm, LastfmClient, UnknownAccount
from typing import Literal as L


async def setup(bot: Bot):
    await bot.add_cog(Spotify(bot))


class Spotify(commands.GroupCog, name="spotify"):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def get_query(
        self, interaction: discord.Interaction, query: Optional[str]
    ) -> str:
        if not query:
            try:
                name = await get_lastfm(self.bot, interaction.user.id)
            except UnknownAccount as e:
                raise ValueError(e)

            info = await LastfmClient(
                self.bot, "2.0", "user.getrecenttracks", "user", name
            )

            if info["recenttracks"]["track"] == []:
                raise ValueError("No recent tracks found for this user.")

            track = info["recenttracks"]["track"][0]
            return f"{track['name']} artist:{track['artist']['#text']}"
        else:
            return query

    async def get_spotify_search_data(
        self,
        query: str,
        mode: Union[L["track"], L["album"]],
    ) -> Dict:
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bot.spotify_key}",
        }

        data = {"q": query, "type": mode, "market": "ES", "limit": "1"}

        async with self.bot.session.get(url, headers=headers, params=data) as r:
            results = await r.json()

        return results

    @app_commands.command(description="Search for a track on spotify")
    @app_commands.describe(query="The name of the track")
    async def track(self, interaction: discord.Interaction, query: Optional[str]):
        await interaction.response.defer(thinking=True)
        try:
            to_search = await self.get_query(interaction, query)
        except ValueError as e:
            await interaction.edit_original_response(content=str(e))
            return

        data = await self.get_spotify_search_data(to_search, "track")
        await interaction.edit_original_response(
            content=data["tracks"]["items"][0]["external_urls"]["spotify"]
        )

    @app_commands.command(description="Search for an album on spotify")
    @app_commands.describe(query="The name of the album")
    async def album(self, interaction: discord.Interaction, query: Optional[str]):
        await interaction.response.defer(thinking=True)
        try:
            to_search = await self.get_query(interaction, query)
        except ValueError as e:
            await interaction.edit_original_response(content=str(e))
            return

        data = await self.get_spotify_search_data(to_search, "album")
        await interaction.edit_original_response(
            content=data["albums"]["items"][0]["external_urls"]["spotify"]
        )
