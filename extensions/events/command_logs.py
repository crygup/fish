from __future__ import annotations

from typing import TYPE_CHECKING

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
