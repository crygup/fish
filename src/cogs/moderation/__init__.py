from __future__ import annotations

from typing import TYPE_CHECKING, List

import discord
from .main import Main
from .mass import Mass

if TYPE_CHECKING:
    from bot import Bot


class Moderation(Main, Mass, name="moderation"):
    """Simple moderation utilities"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="certified_moderator", id=949147443264622643)

    @property
    def aliases(self) -> List[str]:
        return ["mod"]


async def setup(bot: Bot):
    await bot.add_cog(Moderation(bot))
