from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog, is_operational_guild
from core.handoff import is_legacy_instance

from .corn import (
    CORN_EMOJI,
    is_special_reaction_emoji,
    is_special_reaction_target,
)

if TYPE_CHECKING:
    from core import Fishie


class ReactionLogs(Cog):
    """Persist opted-in reactions in their own history table.

    Fishie's automatic corn and configured special reactions intentionally
    remain eligible here. ``CornReacts`` records corn independently in
    ``corn_reacts`` for its existing stats.
    """

    @commands.Cog.listener("on_raw_reaction_add")
    async def on_reaction_log(self, payload: discord.RawReactionActionEvent) -> None:
        if is_legacy_instance(self.bot):
            return
        if is_operational_guild(payload):
            return
        receiver_id = payload.message_author_id
        guild_id = payload.guild_id
        if receiver_id is None or guild_id is None:
            return
        if payload.user_id == receiver_id:
            return

        emoji_id = payload.emoji.id
        emoji_name = payload.emoji.name or str(payload.emoji)
        if not emoji_name:
            return
        is_unicode = emoji_id is None
        emoji_value = str(payload.emoji)
        emoji_attribute = getattr(payload.emoji, "name", None)
        is_corn = emoji_value == CORN_EMOJI or emoji_attribute in {
            CORN_EMOJI,
            "corn",
        }
        fishie_id = getattr(getattr(self.bot, "user", None), "id", None)
        is_fishie_corn = is_corn and payload.user_id == fishie_id
        is_fishie_special = (
            payload.user_id == fishie_id
            and is_special_reaction_target(receiver_id, guild_id)
            and is_special_reaction_emoji(payload.emoji)
        )
        is_fishie_auto = is_fishie_corn or is_fishie_special

        giver = payload.member or self.bot.get_user(payload.user_id)
        # Fishie's automatic corn and configured random reactions are
        # intentionally part of both leaderboards. Other bot reactions remain
        # excluded from the user-opt-in reaction history.
        if giver is not None and giver.bot and not is_fishie_auto:
            return

        cache = self.bot.db_cache
        if not is_fishie_auto and not cache.reaction_tracking_enabled(payload.user_id):
            return

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
