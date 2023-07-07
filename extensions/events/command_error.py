from __future__ import annotations
import sys
import traceback

from typing import TYPE_CHECKING

from discord.ext import commands
from core import Cog
from utils import valid_errors, ignored_errors

if TYPE_CHECKING:
    from context import Context


class CommandErrors(Cog):
    @commands.Cog.listener("on_command_error")
    async def on_command_error(self, ctx: Context, error: commands.CommandError):
        cog = ctx.cog
        command = ctx.command

        if command is None:
            return

        if hasattr(ctx.command, "on_error"):
            return

        if cog and cog._get_overridden_method(cog.cog_command_error) is not None:
            return

        if isinstance(error, ignored_errors):
            return

        ctx.bot.logger.info(
            f'Command {command.name} errored by {ctx.author}. Full content: "{ctx.message.content}"'
        )
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )

        if isinstance(error, valid_errors):
            await ctx.send(str(error))
        else:
            await ctx.send(
                "Well that wasn't supposed to happen, this has been reported."
            )
