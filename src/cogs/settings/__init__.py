from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .server_settings import ServerSettings
from .opt import OptCog
from .link import LinkCog

if TYPE_CHECKING:
    from bot import Bot


class Settings(ServerSettings, OptCog, LinkCog, name="settings"):
    """Settings for the bot"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U00002699\U0000fe0f")


async def setup(bot: Bot):
    await bot.add_cog(Settings(bot))
