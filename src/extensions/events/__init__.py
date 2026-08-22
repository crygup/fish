from __future__ import annotations

import random
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .auto_download import AutoDownload
from .auto_reactions import Reactions
from .auto_upload import AutoUpload
from .command_error import CommandErrors
from .command_logs import CommandLogs
from .corn import CornReacts
from .guilds import Guilds
from .pokemon import Pokemon
from .reactions import ReactionLogs
from .statuses import StatusCog
from .tasks import Tasks
from .xp import XPCog
from .youtube import YouTubeNotifications

if TYPE_CHECKING:
    from core import Fishie


class Events(
    CommandErrors,
    CommandLogs,
    Tasks,
    YouTubeNotifications,
    AutoDownload,
    AutoUpload,
    Pokemon,
    Reactions,
    Guilds,
    XPCog,
    StatusCog,
    CornReacts,
    ReactionLogs,
):
    emoji = discord.PartialEmoji(name="\U0001f3a7")
    hidden: bool = True

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self.cd_mapping = commands.CooldownMapping.from_cooldown(
            1, 5, commands.BucketType.member
        )
        self.xp_cd = commands.CooldownMapping.from_cooldown(
            1, 60, commands.BucketType.user
        )
        self.error_logs = discord.Webhook.from_url(
            bot.config["webhooks"]["error_logs"], session=bot.session
        )

    @commands.Cog.listener("on_message")
    async def on_monark_message(self, message: discord.Message) -> None:
        if message.author.id == 1323759367371231263:
            a = random.randint(0, 500)

            if a == 67:
                await message.channel.send(
                    "22", reference=message.to_reference(fail_if_not_exists=False)
                )


async def setup(bot: Fishie):
    await bot.add_cog(Events(bot))
