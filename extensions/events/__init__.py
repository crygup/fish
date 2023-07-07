from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from core import Cog
from .command_error import CommandErrors
from .command_logs import CommandLogs

if TYPE_CHECKING:
    from core import Fishie


class Events(CommandErrors, CommandLogs, Cog):
    emoji = discord.PartialEmoji(name="\U0001f3a7")


async def setup(bot: Fishie):
    await bot.add_cog(Events())
