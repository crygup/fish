from __future__ import annotations

from typing import TYPE_CHECKING, List, Union

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from .bot import Fishie


class Cog(commands.Cog):
    emoji: Union[discord.Emoji, discord.PartialEmoji]
    aliases: List[str] = []
    bot: Fishie
    hidden: bool = False
