from __future__ import annotations

from typing import TYPE_CHECKING

from utils import fish_discord

from .emojis import Emojis
from .info import Info
from .raw import RawCommands

if TYPE_CHECKING:
    from core import Fishie


class Discord(Emojis, RawCommands, Info):
    """Commands for discord itself"""

    emoji = fish_discord

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot


async def setup(bot: Fishie):
    await bot.add_cog(Discord(bot))
