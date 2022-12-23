from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from .custom_types import *
from .emojis import *
from .errors import *
from .globals import *
from .regexes import *

if TYPE_CHECKING:
    from bot import Bot


class UtilsVars(commands.Cog, name="utils_vars"):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot


async def setup(bot: Bot):
    await bot.add_cog(UtilsVars(bot))
