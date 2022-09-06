import discord
from bot import Bot, Context

from .google import GoogleCommands
from .spotify import SpotifyCommands
from .anime import AnimeCommands


class Tools(GoogleCommands, SpotifyCommands, AnimeCommands, name="search"):
    """Useful tools"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f528")


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))
