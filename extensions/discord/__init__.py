from __future__ import annotations
from io import BytesIO

from typing import TYPE_CHECKING, Union

import discord
from discord.ext import commands

from core import Cog
from utils import fish_discord, TwemojiConverter

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Owner(Cog):
    emoji = fish_discord

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.hybrid_group(name="emoji", fallback="info")
    async def emoji_group(self, ctx: Context, emoji: Union[discord.Emoji, discord.PartialEmoji, TwemojiConverter]):
        if isinstance(emoji, BytesIO):
            await ctx.send(file=discord.File(emoji, "emoji.png"))


async def setup(bot: Fishie):
    await bot.add_cog(Owner(bot))
