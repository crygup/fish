from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from context import Context

    from core import Fishie


class Settings(Cog):
    emoji = discord.PartialEmoji(name="\U00002699\U0000fe0f")

    @commands.command(name="accounts")
    async def accounts(self, ctx: Context):
        ...


async def setup(bot: Fishie):
    await bot.add_cog(Settings())
