from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .downloads import Downloads

if TYPE_CHECKING:
    from core import Fishie


class Tools(Downloads):
    emoji = discord.PartialEmoji(name="\U0001f6e0")


async def setup(bot: Fishie):
    await bot.add_cog(Tools())
