from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from context import Context


class CommandLogs(Cog):
    @commands.Cog.listener("on_command")
    async def on_command(self, ctx: Context):
        if ctx.command is None:
            return

        message = getattr(ctx, "message", None)
        content = getattr(message, "content", "")
        ctx.bot.logger.info(
            f'Command {ctx.command.name} ran by {ctx.author}. Full content: "{content}"'
        )

    @commands.Cog.listener("on_command_completion")
    async def on_command_completion(self, ctx: Context):
        if ctx.command is None:
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
