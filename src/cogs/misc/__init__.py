from __future__ import annotations

from typing import TYPE_CHECKING, List

import discord
import psutil

from .about import About
from .fun import Fun
from .osu import Osu
from .steam import Steam

if TYPE_CHECKING:
    from bot import Bot


class Miscellaneous(About, Fun, Osu, Steam, name="miscellaneous"):
    """Miscellaneous commands."""

    def __init__(self, bot: Bot):
        super().__init__(bot)
        self.process = psutil.Process()

        perms = discord.Permissions(1074055232)
        self.invite_url = discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot",))  # type: ignore

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="\U0001f4a0")

    @property
    def aliases(self) -> List[str]:
        return ["misc"]


async def setup(bot: Bot):
    await bot.add_cog(Miscellaneous(bot))
