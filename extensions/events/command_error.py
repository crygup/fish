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

        msg = (
            str(error)
            if isinstance(error, valid_errors)
            else "Well that wasn't supposed to happen, this has been reported."
        )

        if ctx.interaction:
            return await ctx.interaction.followup.send(msg)

        await ctx.send(msg)

        excinfo = "".join(
            traceback.format_exception(
                type(error), error, error.__traceback__, chain=False
            )
        )

        embed = discord.Embed(title="Command Error", colour=self.bot.embedcolor)
        embed.add_field(name="Name", value=command.qualified_name)
        embed.add_field(name="Author", value=f"{ctx.author} (ID: {ctx.author.id})")

        fmt = f"Channel: {ctx.channel} (ID: {ctx.channel.id})"

        if ctx.guild:
            fmt = f"{fmt}\nGuild: {ctx.guild} (ID: {ctx.guild.id})"

        embed.add_field(name="Location", value=fmt, inline=False)
        embed.add_field(
            name="Content", value=textwrap.shorten(ctx.message.content, 512)
        )

        embed.description = f"```py\n{excinfo}\n```"
        embed.timestamp = discord.utils.utcnow()
        await self.error_logs.send(embed=embed)
