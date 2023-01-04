from __future__ import annotations

from typing import TYPE_CHECKING, Union

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

    async def do_error(self, ctx: Context, error: str):
        if ctx.interaction:
            try:
                await ctx.interaction.response.send_message(
                    error, allowed_mentions=self.mentions
                )
            except (discord.HTTPException, discord.InteractionResponded):
                await ctx.interaction.edit_original_response(
                    content=error, allowed_mentions=self.mentions
                )
        else:
            await ctx.send(error, allowed_mentions=self.mentions)

    @commands.Cog.listener("on_command_error")
    async def on_command_error(
        self,
        ctx: Context,
        error: Union[commands.CommandError, commands.HybridCommandError],
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

        if isinstance(error, commands.DisabledCommand):
            if ctx.command is None:
                return

            return await self.do_error(
                ctx, f"{ctx.command.name.capitalize()} has been disabled temporarily."
            )

        elif isinstance(error, (commands.CommandOnCooldown, RateLimitExceeded)):
            try:
                await ctx.message.add_reaction("\N{HOURGLASS}")
            except:
                pass

        elif isinstance(error, DownloadError):
            await self.do_error(ctx, "Invalid video url.")

        elif isinstance(error, DevError):
            channel: discord.TextChannel = self.bot.get_channel(
                989112775487922237
            )  # type:ignore
            liz = await get_or_fetch_user(self.bot, 766953372309127168)
            return await ctx.send(f"{liz.mention} \n{error}")

        elif (
            isinstance(error, discord.HTTPException)
            and ctx.command
            and ctx.command.name == "download"
        ):
            await self.do_error(ctx, "Video too large, try a shorter video.")

        elif isinstance(error, SEND):
            await self.do_error(ctx, str(error))

        elif isinstance(error, IGNORED):
            return

        else:
            if ctx.command is None:
                return

            await self.bot.send_error(ctx, error)
