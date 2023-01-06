from __future__ import annotations

from typing import TYPE_CHECKING, List

import discord

from .chart import Chart
from .fm import FmCommand
from .top import TopCommands

if TYPE_CHECKING:
    from bot import Bot


class Lastfm(FmCommand, Chart, TopCommands, name="lastfm"):
    """Last.fm integration"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="lastfm", id=1006848047923351612)


async def setup(bot: Bot):
    await bot.add_cog(Lastfm(bot))
