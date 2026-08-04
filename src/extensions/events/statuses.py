from __future__ import annotations

import discord
from discord.ext import commands

from core import Cog


class StatusCog(Cog):
    _TRACKED_STATUSES = frozenset({"offline", "idle", "online", "dnd"})

    async def store_status(self, member: discord.Member, status: str) -> None:
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(
                            $1::bigint::text || ':' || $2::bigint::text,
                            0
                        )
                    )
                    """,
                    member.id,
                    member.guild.id,
                )
                await connection.execute(
                    """
                    UPDATE user_status_history
                    SET ended_at = now()
                    WHERE user_id = $1
                      AND guild_id = $2
                      AND ended_at IS NULL
                      AND status <> $3
                    """,
                    member.id,
                    member.guild.id,
                    status,
                )
                await connection.execute(
                    """
                    INSERT INTO user_status_history (
                        user_id,
                        guild_id,
                        status,
                        started_at
                    )
                    SELECT $1, $2, $3, now()
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM user_status_history
                        WHERE user_id = $1
                          AND guild_id = $2
                          AND status = $3
                          AND ended_at IS NULL
                    )
                    """,
                    member.id,
                    member.guild.id,
                    status,
                )
                # Keep the original table current as a compact last-seen cache
                # and a ready fallback for the existing uptime behavior.
                await connection.execute(
                    """
                    INSERT INTO user_statuses (user_id, guild_id, status, last_seen)
                    VALUES ($1, $2, $3, now())
                    ON CONFLICT (user_id, guild_id, status)
                    DO UPDATE SET last_seen = EXCLUDED.last_seen
                    """,
                    member.id,
                    member.guild.id,
                    status,
                )

    @commands.Cog.listener("on_presence_update")
    async def _on_presence_update(self, before: discord.Member, after: discord.Member):
        if self.bot.user and after.id == self.bot.user.id:
            return
        if self.bot.db_cache.user_tracking_opted_out(after.id, "status"):
            return

        if before.status == after.status:
            return

        status = str(after.status)
        if status == "invisible":
            status = "offline"
        if status not in self._TRACKED_STATUSES:
            return

        await self.store_status(after, status)
