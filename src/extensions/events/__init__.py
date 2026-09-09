from __future__ import annotations

import random
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import is_operational_guild
from core.handoff import is_legacy_instance

from .auto_download import AutoDownload
from .auto_reactions import Reactions
from .auto_upload import AutoUpload
from .boards import BoardEvents
from .command_error import CommandErrors
from .command_logs import CommandLogs
from .corn import (
    CORN_EMOJI,
    CORN_REACTION_USER_IDS,
    SPECIAL_REACTION_GROUPS,
    CornReacts,
    is_special_reaction_target,
)
from .guilds import Guilds
from .pokemon import Pokemon
from .reactions import ReactionLogs
from .statuses import StatusCog
from .tasks import Tasks
from .xp import XPCog

if TYPE_CHECKING:
    from core import Fishie


class Events(
    CommandErrors,
    CommandLogs,
    Tasks,
    AutoDownload,
    AutoUpload,
    Pokemon,
    Reactions,
    Guilds,
    XPCog,
    StatusCog,
    BoardEvents,
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

    async def cog_load(self) -> None:
        # ``Tasks.cog_load`` starts the durable notification/cleanup loops.
        # Guild capacity is owned by ``Guilds`` but this class is a single
        # mixed-in cog, so start its loop here after the parent hooks finish.
        await super().cog_load()
        if is_legacy_instance(self.bot):
            return
        capacity_task = getattr(self, "capacity_check_task", None)
        if capacity_task is not None and not capacity_task.is_running():
            capacity_task.start()

    async def cog_unload(self) -> None:
        capacity_task = getattr(self, "capacity_check_task", None)
        if capacity_task is not None:
            capacity_task.cancel()
        await super().cog_unload()

    @commands.Cog.listener("on_message")
    async def on_corn_message(self, message: discord.Message) -> None:
        """Occasionally react to messages from the configured corn targets.

        This is intentionally kept as a tiny, in-memory easter egg rather
        than a database-backed feature.  The target list is explicit so the
        reaction cannot unexpectedly trigger for everyone, and using
        ``randrange(800)`` gives each eligible message exactly a 1/800 chance.
        """

        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(message):
            return
        guild_id = message.guild.id if message.guild is not None else None
        if not is_special_reaction_target(message.author.id, guild_id):
            return
        # Avoid attempting to react to a message authored by Fishie itself if
        # its own ID is ever added to the target list in the future.
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        if random.randrange(800) != 0:
            return
        try:
            await message.add_reaction(CORN_EMOJI)
        except (
            discord.Forbidden,
            discord.HTTPException,
            TypeError,
            ValueError,
        ):
            # Missing Add Reactions permission (or a deleted message) should
            # not turn this optional reaction into an on_message failure.
            return

    @commands.Cog.listener("on_message")
    async def on_special_reaction_message(self, message: discord.Message) -> None:
        """Occasionally add one configured reaction to an eligible message."""

        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(message):
            return
        guild_id = message.guild.id if message.guild is not None else None
        if not is_special_reaction_target(message.author.id, guild_id):
            return
        # Avoid reacting to Fishie's own messages if its ID is ever included
        # in the explicit target list.
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        if random.randrange(1000) != 0:
            return

        try:
            # A grouped outcome is applied in tuple order; this guarantees
            # that the 6️⃣/7️⃣ pair is always added as 6️⃣ first, then 7️⃣.
            for emoji in random.choice(SPECIAL_REACTION_GROUPS):
                await message.add_reaction(emoji)
        except (
            discord.Forbidden,
            discord.HTTPException,
            TypeError,
            ValueError,
        ):
            # This optional event must not make normal message handling fail
            # when the channel disallows reactions or the message disappears.
            return


async def setup(bot: Fishie):
    await bot.add_cog(Events(bot))
