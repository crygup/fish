from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

from .afk import AfkCommands
from .downloads import DownloadCommands
from .money import MoneyCommands
from .other import OtherCommands
from .reminder import ReminderCommands
from .tags import TagCommands

# from .feed import FeedCommands # it's not working at the moment due to random shut downs of the live twitter client so until that's fixed it's shut down

if TYPE_CHECKING:
    from bot import Bot
    from utils import PGTimer


class Tools(
    TagCommands,
    DownloadCommands,
    OtherCommands,
    AfkCommands,
    MoneyCommands,
    ReminderCommands,
    # FeedCommands,
    name="tools",
):
    """Useful tools"""

    def __init__(self, bot: Bot):
        super().__init__(bot)
        self.currently_downloading: list[str] = []
        self._have_data = asyncio.Event()
        self._current_timer: PGTimer | None = None

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f528")


async def setup(bot: Bot):
    await bot.add_cog(Tools(bot))
