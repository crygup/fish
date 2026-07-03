from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

# from .reminders import MudaeReminders
from .sphere import SphereCog
from utils import mudae_circle
if TYPE_CHECKING:
    from core import Fishie


class Mudae(SphereCog):
    """Mudae tools."""

    emoji = mudae_circle

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot


async def setup(bot: Fishie):
    await bot.add_cog(Mudae(bot))
