from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from discord.ext.commands import Cog

if TYPE_CHECKING:
    from bot import Bot
    from utils import PGTimer


class CogBase(Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
