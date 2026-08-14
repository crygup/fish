from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import SILENT_COMMAND_USERS, Cog

if TYPE_CHECKING:
    from context import Context


class CommandLogs(Cog):
    async def _record_command(
        self,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        message_id: int | None,
        command_name: str,
    ) -> None:
        if user_id in SILENT_COMMAND_USERS.get(command_name, frozenset()):
            return
        if self.bot.db_cache.user_tracking_opted_out(user_id, "commands"):
            return
        await self.bot.pool.execute(
            """
            INSERT INTO command_logs(
                user_id, guild_id, channel_id, message_id, command, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            user_id,
            guild_id,
            channel_id,
            message_id,
            command_name,
            discord.utils.utcnow(),
        )

    @commands.Cog.listener("on_command")
    async def on_command(self, ctx: Context):
        if ctx.command is None:
            return
        if ctx.author.id in SILENT_COMMAND_USERS.get(
            ctx.command.qualified_name.casefold(), frozenset()
        ):
            return

        ctx.bot.logger.info(
            "Command %s invoked by user_id=%s guild_id=%s channel_id=%s",
            ctx.command.qualified_name,
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            getattr(ctx.channel, "id", None),
        )

    @commands.Cog.listener("on_command_completion")
    async def on_command_completion(self, ctx: Context):
        if ctx.command is None:
            return
        message = getattr(ctx, "message", None)
        channel = getattr(ctx, "channel", None)
        await self._record_command(
            user_id=ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=getattr(channel, "id", None),
            message_id=getattr(message, "id", None),
            # ``name`` only contains the leaf (for example ``py``).  The
            # qualified name keeps every group level, including nested groups.
            command_name=ctx.command.qualified_name.casefold(),
        )

    @commands.Cog.listener("on_app_command_completion")
    async def on_app_command_completion(self, interaction, command) -> None:
        """Record slash commands with the same qualified names as text commands."""

        qualified_name = command.qualified_name.casefold()
        await self._record_command(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            # Interactions do not have a message in DMs, but the interaction
            # ID still gives this log row a stable source identifier.
            message_id=interaction.id,
            command_name=qualified_name,
        )
