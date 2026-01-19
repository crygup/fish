from __future__ import annotations

import sys
import textwrap
import traceback
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import Cog
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

        if isinstance(error, commands.HybridCommandError):
            await ctx.send(
                "Something went wrong while processing this command, try again?"
            )
            await self.bot.log_error(error)
            return

        removed_keys = (
            str(error)
            .replace(ctx.bot.config["keys"]["lastfm"], "REDACTED_KEY")
            .replace(ctx.bot.config["keys"]["spotify_id"], "REDACTED_KEY")
        )
        await ctx.send(removed_keys)

        await self.bot.log_error(error)
