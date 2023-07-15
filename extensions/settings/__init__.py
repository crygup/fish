from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

from .logging import Logging
from .server import Server
if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Settings(Logging, Server):
    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot


async def setup(bot: Fishie):
    await bot.add_cog(Settings(bot))
