from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from .classes import *
from .functions import *
from .timer import *
from .roblox import *

if TYPE_CHECKING:
    from bot import Bot


class UtilsHelpers(commands.Cog, name="utils_helpers"):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot


async def setup(bot: Bot):
    await bot.add_cog(UtilsHelpers(bot))
