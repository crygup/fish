from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .manipulation import Manipulation
from .waifu import Waifus

if TYPE_CHECKING:
    from bot import Bot


class Image(Waifus, Manipulation, name="image"):
    """Image related stuff"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f3a8")


async def setup(bot: Bot):
    await bot.add_cog(Image(bot))
