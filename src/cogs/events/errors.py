from __future__ import annotations
import sys
import traceback

from typing import TYPE_CHECKING, Any, Union

import discord
from discord.ext import commands
from yt_dlp import DownloadError

from utils import IGNORED, SEND, RateLimitExceeded, DevError, get_or_fetch_user

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(ErrorEvents(bot))


class ErrorEvents(commands.Cog, name="error_events"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.mentions = discord.AllowedMentions(
            users=True, roles=False, everyone=False, replied_user=False
        )

    @commands.Cog.listener("on_error")
    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any):
        traceback_string = "".join(
            traceback.format_exception(*(einfo := sys.exc_info()))
        )
        self.bot.logger.error(
            "Unhandled exception in event %s", event_method, exc_info=einfo
        )

        embed = discord.Embed(title=f"{event_method} Error", colour=self.bot.embedcolor)

        embed.description = f"```py\n{traceback_string}\n```"
        embed.timestamp = discord.utils.utcnow()
        await self.bot.webhooks["error_logs"].send(embed=embed)

    @commands.Cog.listener("on_command_error")
    async def on_command_error(
        self,
        ctx: Context,
        error: Union[commands.CommandError, Exception, commands.HybridCommandError],
    ):
        if hasattr(ctx.command, "on_error"):
            return

        if isinstance(error, commands.HybridCommandError):
            error = error.original.original  # type: ignore

        cog = ctx.cog
        if cog and cog._get_overridden_method(cog.cog_command_error) is not None:
            return

        if (
            ctx.command
            and ctx.command.cooldown
            and not isinstance(error, commands.CommandOnCooldown)
        ):
            ctx.command.reset_cooldown(ctx)

        error = getattr(error, "original", error)

        excinfo = "".join(
            traceback.format_exception(
                type(error), error, error.__traceback__, chain=False
            )
        )

        bucket = self.bot.error_message_cooldown.get_bucket(ctx.message)

        if bucket is None:
            return

        retry_after = bucket.update_rate_limit()

        if retry_after:
            return

        if isinstance(error, commands.DisabledCommand):
            return await ctx.send("Sorry this command is disabled currently.")

        if isinstance(error, (commands.CommandOnCooldown, RateLimitExceeded)):
            try:
                await ctx.message.add_reaction("\N{HOURGLASS}")
            except:
                pass

        if isinstance(error, DownloadError):
            # await self.bot.post_error(ctx, excinfo)
            return await ctx.send(
                "Sorry, something went wrong while downloading, try again later?"
            )

        if isinstance(error, DevError):
            channel: discord.TextChannel = self.bot.get_channel(
                989112775487922237
            )  # type: ignore
            liz = await get_or_fetch_user(self.bot, 766953372309127168)
            return await channel.send(f"{liz.mention} \n{error}")

        if isinstance(error, SEND):
            return await ctx.send(str(error))

        if isinstance(error, IGNORED):
            return

        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )
        await self.bot.post_error(ctx, excinfo)
