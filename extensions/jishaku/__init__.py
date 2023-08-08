from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from jishaku.cog import OPTIONAL_FEATURES, STANDARD_FEATURES
from jishaku.features.baseclass import Feature

from core import Cog
from utils import fish_owner
from discord.ext import commands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Jishaku(Cog, *OPTIONAL_FEATURES, *STANDARD_FEATURES):
    emoji = fish_owner
    hidden: bool = True

    @Feature.Command(parent="jsk", name="test")
    async def test(self, ctx: Context):
        raise commands.BadArgument("Test error")


async def setup(bot: Fishie):
    await bot.add_cog(Jishaku(bot=bot))
