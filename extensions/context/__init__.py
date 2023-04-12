from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord.ext import commands

if TYPE_CHECKING:
    from core import Fishie


class Context(commands.Context["Fishie"]):
    session: aiohttp.ClientSession
    bot: "Fishie"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.session = self.bot.session


class DuckGuildContext(Context):
    author: discord.Member  # type: ignore
    channel: discord.abc.GuildChannel  # type: ignore


async def setup(bot: "Fishie") -> None:
    bot.context_cls = Context


async def teardown(bot: Fishie) -> None:
    bot.context_cls = commands.Context
