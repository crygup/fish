from __future__ import annotations

from typing import TYPE_CHECKING, List

import discord

from .about import About
from .fun import Fun
from .steam import Steam

if TYPE_CHECKING:
    from bot import Bot


class Miscellaneous(About, Fun, Steam, name="miscellaneous"):
    """Miscellaneous commands."""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f4a0")

    @property
    def aliases(self) -> List[str]:
        return ["misc"]


async def setup(bot: Bot):
    await bot.add_cog(Miscellaneous(bot))
