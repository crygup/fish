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

        ctx.bot.logger.info(
            f'Command {ctx.command.name} ran by {ctx.author}. Full content: "{ctx.message.content}"'
        )

    @commands.Cog.listener("on_command_completion")
    async def on_command_comletion(self, ctx: Context):
        if ctx.command is None:
            return

        sql = """
        INSERT INTO command_logs(user_id, guild_id, channel_id, message_id, command, created_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """

        await self.bot.pool.execute(
            sql,
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            ctx.message.id,
            ctx.command.name,
            discord.utils.utcnow(),
        )
