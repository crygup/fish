from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import SILENT_COMMAND_USERS, Cog

if TYPE_CHECKING:
    from context import Context


class CommandLogs(Cog):
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
        if ctx.author.id in SILENT_COMMAND_USERS.get(
            ctx.command.qualified_name.casefold(), frozenset()
        ):
            return
        if "commands" in self.bot.db_cache.get_opted_out(ctx.author.id):
            return

        sql = """
        INSERT INTO command_logs(user_id, guild_id, channel_id, message_id, command, created_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """

        message = getattr(ctx, "message", None)
        channel = getattr(ctx, "channel", None)
        await self.bot.pool.execute(
            sql,
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            getattr(channel, "id", None),
            getattr(message, "id", None),
            ctx.command.name,
            discord.utils.utcnow(),
        )
