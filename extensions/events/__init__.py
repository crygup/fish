from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .command_error import CommandErrors
from .command_logs import CommandLogs
from .spotify import Spotify

if TYPE_CHECKING:
    from core import Fishie


class Events(CommandErrors, CommandLogs, Spotify):
    emoji = discord.PartialEmoji(name="\U0001f3a7")


async def setup(bot: Fishie):
    await bot.add_cog(Events(bot))
