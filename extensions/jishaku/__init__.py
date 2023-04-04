from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands
from jishaku.cog import OPTIONAL_FEATURES, STANDARD_FEATURES
from jishaku.features.baseclass import Feature

from core import Cog

if TYPE_CHECKING:
    from core import Fishie


class Jishaku(Cog, *OPTIONAL_FEATURES, *STANDARD_FEATURES):  # type: ignore
    @Feature.Command(parent="jsk", name="test")
    async def test(self, ctx: commands.Context[Fishie]):
        await ctx.send("This is a test command!")


async def setup(bot: Fishie):
    await bot.add_cog(Jishaku(bot=bot))
