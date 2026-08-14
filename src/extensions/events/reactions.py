from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie


class ReactionLogs(Cog):
    """Persist opted-in reactions in their own history table.

    Corn reactions intentionally remain eligible here. ``CornReacts`` records
    the same event independently in ``corn_reacts`` for its existing stats.
    """

    @commands.Cog.listener("on_raw_reaction_add")
    async def on_reaction_log(self, payload: discord.RawReactionActionEvent) -> None:
        receiver_id = payload.message_author_id
        guild_id = payload.guild_id
        if receiver_id is None or guild_id is None:
            return
        if payload.user_id == receiver_id:
            return

        giver = payload.member or self.bot.get_user(payload.user_id)
        if giver is not None and giver.bot:
            return

        cache = self.bot.db_cache
        if not cache.reaction_tracking_enabled(payload.user_id):
            return

        emoji_id = payload.emoji.id
        emoji_name = payload.emoji.name or str(payload.emoji)
        if not emoji_name:
            return
        is_unicode = emoji_id is None
        user_created_at = discord.utils.snowflake_time(payload.user_id)
        guild_created_at = discord.utils.snowflake_time(guild_id)

        try:
            await self.bot.pool.execute(
                "INSERT INTO reaction_logs ("
                "giver_id, receiver_id, guild_id, channel_id, message_id, "
                "emoji_name, emoji_id, unicode, user_created_at, guild_created_at"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
                "ON CONFLICT DO NOTHING",
                payload.user_id,
                receiver_id,
                guild_id,
                payload.channel_id,
                payload.message_id,
                emoji_name,
                emoji_id,
                is_unicode,
                user_created_at,
                guild_created_at,
            )
        except Exception:
            self.bot.logger.exception("Failed to record reaction log")


async def setup(bot: Fishie):
    await bot.add_cog(ReactionLogs())
