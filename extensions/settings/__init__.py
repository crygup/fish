from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .logging import Logging
from .server import Server

if TYPE_CHECKING:
    from core import Fishie


class Settings(Logging, Server):
    """User and server settings"""

    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot


async def setup(bot: Fishie):
    await bot.add_cog(Settings(bot))
