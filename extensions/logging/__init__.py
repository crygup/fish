from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from core import Cog

from .avatars import Avatars
from .commands import Commands
from .guild import Guild
from .users import User

if TYPE_CHECKING:
    from core import Fishie


class Logging(Avatars, Guild, User, Commands):
    """Google data tracking on discord!"""

    emoji = discord.PartialEmoji(name="\U0001fab5")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot


async def setup(bot: Fishie):
    await bot.add_cog(Logging(bot))
