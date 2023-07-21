from __future__ import annotations

from typing import TYPE_CHECKING, Union

import discord
from discord.ext import commands

from utils import fish_discord

from .emojis import Emojis
from .raw import RawCommands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Discord(Emojis, RawCommands):
    """Commands for discord itself"""

    emoji = fish_discord

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot


async def setup(bot: Fishie):
    await bot.add_cog(Discord(bot))
