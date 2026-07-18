from __future__ import annotations

import sys
import textwrap
import traceback
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog, SILENT_COMMAND_USERS
from utils import ignored_errors, valid_errors

if TYPE_CHECKING:
    from context import Context


class CommandErrors(Cog):
    error_logs: discord.Webhook

    @commands.Cog.listener("on_command_error")
    async def on_command_error(self, ctx: Context, error: commands.CommandError):
        cog = ctx.cog
        command = ctx.command

        if command is None:
            return

        if ctx.author.id in SILENT_COMMAND_USERS.get(
            command.qualified_name.casefold(), frozenset()
        ):
            return

        if hasattr(ctx.command, "on_error"):
            return

        if cog and cog._get_overridden_method(cog.cog_command_error) is not None:
            return

        if isinstance(error, ignored_errors):
            return

        message = getattr(ctx, "message", None)
        content = getattr(message, "content", "")
        ctx.bot.logger.info(
            f'Command {command.name} errored by {ctx.author}. Full content: "{content}"'
        )
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )

        # if isinstance(error, commands.HybridCommandError):
        #     await ctx.send(
        #         "Something went wrong while processing this command, try again?"
        #     )
        #     await self.bot.log_error(error)
        #     return

        try:
            error_str = self.bot.redact(str(error))
            await ctx.send(error_str)
        except Exception as e:
            ctx.bot.logger.error(
                f"on_command_error handler itself failed: {e.__class__.__name__}: {e}"
            )
            try:
                await ctx.send("An unexpected error occurred.")
            except Exception:
                pass
        await self.bot.log_error(error)
