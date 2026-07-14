from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from cachetools import TTLCache
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie

CORN_EMOJI = "\U0001f33d"


class CornReacts(Cog):
    """Track corn emoji reactions, one per giver per message."""

    def __init__(self) -> None:
        self._seen: TTLCache[tuple[int, int], bool] = TTLCache[tuple[int, int], bool](
            maxsize=10_000, ttl=600.0
        )

    @commands.Cog.listener("on_raw_reaction_add")
    async def on_corn_react(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == payload.message_author_id:
            return

        emoji = str(payload.emoji)
        if emoji != CORN_EMOJI and (
            not hasattr(payload.emoji, "name") or payload.emoji.name != "corn"
        ):
            return

        key = (payload.user_id, payload.message_id)
        if key in self._seen:
            return
        self._seen[key] = True

        await self.bot.pool.execute(
            "INSERT INTO corn_reacts (receiver_id, giver_id, guild_id, message_id, channel_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            payload.message_author_id,
            payload.user_id,
            payload.guild_id,
            payload.message_id,
            payload.channel_id,
        )


async def setup(bot: Fishie):
    await bot.add_cog(CornReacts())
