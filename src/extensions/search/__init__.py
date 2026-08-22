from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .google import Google
from .letterboxd import Letterboxd
from .roblox import Roblox
from .screenshot import Screenshot
from .spotify import Spotify
from .steam import Steam
from .twitch import Twitch

if TYPE_CHECKING:
    from core import Fishie


class Search(Google, Letterboxd, Roblox, Screenshot, Steam, Spotify, Twitch):
    """Search Google, Steam, Letterboxd & more"""

    emoji = discord.PartialEmoji(name="🌐")

    def __init__(self, bot: Fishie) -> None:
        super().__init__(bot)


async def setup(bot: Fishie) -> None:
    await bot.add_cog(Search(bot))
