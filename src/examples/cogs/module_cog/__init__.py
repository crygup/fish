from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .cog import ExampleCog

if TYPE_CHECKING:
    from bot import Bot


class Example(ExampleCog, name="example"):
    """Example description"""

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name="owner", id=949147456376033340)


async def setup(bot: Bot):
    await bot.add_cog(Example(bot))
