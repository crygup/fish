from __future__ import annotations

from typing import TYPE_CHECKING

from jishaku.cog import OPTIONAL_FEATURES, STANDARD_FEATURES
from jishaku.features.baseclass import Feature

from core import Cog

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Jishaku(Cog, *OPTIONAL_FEATURES, *STANDARD_FEATURES):
    @Feature.Command(parent="jsk", name="test")
    async def test(self, ctx: Context):
        await ctx.send("This is a test command!")


async def setup(bot: Fishie):
    await bot.add_cog(Jishaku(bot=bot))
