import discord
from bot import Bot, Context
from discord.ext import commands
from utils import IGNORED, SEND, RateLimitExceeded
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
    async def on_command_error(self, ctx: Context, error: commands.CommandError):
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

        elif isinstance(error, (commands.CommandOnCooldown, RateLimitExceeded)):
            try:
                await ctx.message.add_reaction("\N{HOURGLASS}")
            except:
                pass

        elif isinstance(error, DownloadError):
            await ctx.send("Invalid video url.")

        elif (
            isinstance(error, discord.HTTPException)
            and ctx.command
            and ctx.command.name == "download"
        ):
            await ctx.send("Video too large, try a shorter video.")

        elif isinstance(error, SEND):
            await ctx.send(str(error), allowed_mentions=self.mentions)

        elif isinstance(error, IGNORED):
            return

        else:
            if ctx.command is None:
                return

            await self.bot.send_error(ctx, error)
