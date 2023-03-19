from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bot import Bot

__all__ = ["BaseCog"]


class BaseCog(commands.Cog):
    bot: Bot
    emoji: Optional[discord.PartialEmoji]

    def __init__(self, bot: Bot):
        self.bot = bot

    @property
    def aliases(self) -> List[str]:
        return []
