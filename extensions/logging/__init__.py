from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from core import Cog
from .avatars import Avatars

if TYPE_CHECKING:
    from core import Fishie


class Logging(Avatars):
    emoji = discord.PartialEmoji(name="\U0001fab5")


async def setup(bot: Fishie):
    await bot.add_cog(Logging(bot))
