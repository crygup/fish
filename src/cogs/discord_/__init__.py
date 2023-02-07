from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .info import InfoCommands
from .other import OtherCommands
from .search import SearchCommand
from .user import UserCommands
from .emojis import Emojis

if TYPE_CHECKING:
    from bot import Bot


class Discord(
    SearchCommand,
    InfoCommands,
    UserCommands,
    OtherCommands,
    Emojis,
    name="discord",
):
    """Commands for discord itself"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="discord", id=1006848754944593921)


async def setup(bot: Bot):
    await bot.add_cog(Discord(bot))
