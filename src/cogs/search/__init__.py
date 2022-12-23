from typing import TYPE_CHECKING

import discord

from .anime import AnimeCommands
from .google import GoogleCommands
from .spotify import SpotifyCommands
from .youtube import YoutubeCommands

if TYPE_CHECKING:
    from bot import Bot


class Tools(
    GoogleCommands, SpotifyCommands, AnimeCommands, YoutubeCommands, name="search"
):
    """Search info on the web"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f50d")


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))
