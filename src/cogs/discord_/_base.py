from __future__ import annotations

from discord.ext.commands import Cog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import Bot


class CogBase(Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
