from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .auto_download import AutoDownload
from .auto_reactions import Reactions
from .command_error import CommandErrors
from .command_logs import CommandLogs
from .guilds import Guilds
from .pokemon import Pokemon
from .tasks import Tasks

if TYPE_CHECKING:
    from core import Fishie


class Events(
    CommandErrors, CommandLogs, Tasks, AutoDownload, Pokemon, Reactions, Guilds
):
    emoji = discord.PartialEmoji(name="\U0001f3a7")
    hidden: bool = True

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self.cd_mapping = commands.CooldownMapping.from_cooldown(
            1, 5, commands.BucketType.member
        )


async def setup(bot: Fishie):
    await bot.add_cog(Events(bot))
