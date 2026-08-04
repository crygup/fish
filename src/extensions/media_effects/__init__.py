from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .commands import (
    Images,
    describe_media_parameters,
    describe_text_numeric_ranges,
)

if TYPE_CHECKING:
    from core import Fishie


class MediaEffects(Images, name="Media Effects"):
    """Image, GIF, video, and audio manipulation commands."""

    emoji = discord.PartialEmoji(name="\U0001f5bc\ufe0f")
    aliases = ["media", "effects"]

    def __init__(self, bot: Fishie) -> None:
        self.bot = bot
        self._remove_audio_overlay_app_command()
        for command in self.get_app_commands():
            describe_media_parameters(command)
        for command in self.walk_commands():
            app_command = getattr(command, "app_command", None)
            if app_command is not None:
                describe_media_parameters(app_command)
            describe_text_numeric_ranges(command)
        self._rebalance_effect_app_commands()


async def setup(bot: Fishie) -> None:
    await bot.add_cog(MediaEffects(bot))
