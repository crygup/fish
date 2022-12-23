from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from .converters import *
from .paginator import *
from .views import *

if TYPE_CHECKING:
    from bot import Bot


class UtilsViews(commands.Cog, name="utils_views"):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot


async def setup(bot: Bot):
    await bot.add_cog(UtilsViews(bot))
