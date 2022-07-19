import sys
import textwrap
import traceback

import discord
from bot import Bot
from discord.ext import commands
from utils import IGNORED, SEND
from yt_dlp import DownloadError


async def setup(bot: Bot):
    await bot.add_cog(ErrorEvents(bot))


class ErrorEvents(commands.Cog, name="error_events"):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.mentions = discord.AllowedMentions(
            users=True, roles=False, everyone=False, replied_user=False
        )

    @commands.Cog.listener("on_command_error")
    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ):
        if hasattr(ctx.command, "on_error"):
            return

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

            return await ctx.send(
                f"{ctx.command.name.capitalize()} has been disabled temporarily."
            )

        elif isinstance(error, commands.CommandOnCooldown):
            try:
                await ctx.message.add_reaction("\N{HOURGLASS}")
            except:
                pass

        elif isinstance(error, DownloadError):
            await ctx.send("Invalid video url.")

        elif isinstance(error, SEND):
            await ctx.send(str(error), allowed_mentions=self.mentions)

        elif isinstance(error, IGNORED):
            return

        else:
            if ctx.command is None:
                return

            await ctx.send(f"An unhandled error occured, this error has been reported.")
            embed = discord.Embed(title="Command Error", colour=self.bot.embedcolor)
            embed.add_field(name="Name", value=ctx.command.qualified_name)
            embed.add_field(name="Author", value=f"{ctx.author} (ID: {ctx.author.id})")

            fmt = f"Channel: {ctx.channel} (ID: {ctx.channel.id})"

            if ctx.guild:
                fmt = f"{fmt}\nGuild: {ctx.guild} (ID: {ctx.guild.id})"

            embed.add_field(name="Location", value=fmt, inline=False)
            embed.add_field(
                name="Content", value=textwrap.shorten(ctx.message.content, 512)
            )

            exc = "".join(
                traceback.format_exception(
                    type(error), error, error.__traceback__, chain=False
                )
            )
            traceback.print_exception(
                type(error), error, error.__traceback__, file=sys.stderr
            )
            embed.description = f"```py\n{exc}\n```"
            embed.timestamp = discord.utils.utcnow()
            await self.bot.webhooks["error_logs"].send(embed=embed)
            return
