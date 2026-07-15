from __future__ import annotations

from typing import TYPE_CHECKING

from utils import fish_discord2

from .emojis import Emojis
from .info import Info
from .raw import RawCommands
from .pins import Pinboard

if TYPE_CHECKING:
    from core import Fishie


class Discord(Emojis, RawCommands, Info, Pinboard):
    """Commands for discord itself"""

    emoji = fish_discord2

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot


async def setup(bot: Fishie):
    await bot.add_cog(Discord(bot))
