from __future__ import annotations

import datetime
import sys
import traceback
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from core import SILENT_COMMAND_USERS, Cog, is_operational_guild
from utils import ignored_errors

if TYPE_CHECKING:
    from context import Context


class CommandErrors(Cog):
    error_logs: discord.Webhook

    @staticmethod
    def _cooldown_message(retry_after: float) -> str:
        """Render cooldown expiry as Discord's relative timestamp."""

        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=max(0.0, float(retry_after))
        )
        return f"You are on cooldown. Try again {discord.utils.format_dt(expires_at, 'R')}."

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
            if isinstance(error, commands.CommandOnCooldown):
                error_str = self._cooldown_message(error.retry_after)
            else:
                error_str = self.bot.redact(str(error))
            await ctx.send(
                error_str,
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=ctx.interaction is not None,
            )
        except Exception as e:
            ctx.bot.logger.error(
                f"on_command_error handler itself failed: {e.__class__.__name__}: {e}"
            )
            try:
                await ctx.send(
                    "An unexpected error occurred.",
                    ephemeral=ctx.interaction is not None,
                )
            except Exception:
                pass
        # The operations guild is reserved for private review/upload work and
        # is excluded from external error telemetry just like the other event
        # streams.  Keep the user-facing error above so a failed command still
        # receives a useful response there.
        if not is_operational_guild(ctx.guild):
            await self.bot.log_error(
                error,
                context=ctx,
                interaction=getattr(ctx, "interaction", None),
            )
