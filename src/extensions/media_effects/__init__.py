from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .commands import Images

if TYPE_CHECKING:
    from core import Fishie


class MediaEffects(Images, name="Media Effects"):
    """Image, GIF, video, and audio manipulation commands."""

    emoji = discord.PartialEmoji(name="\U0001f5bc\ufe0f")
    aliases = ["media", "effects"]

    def __init__(self, bot: Fishie) -> None:
        self.bot = bot


async def setup(bot: Fishie) -> None:
    await bot.add_cog(MediaEffects(bot))
