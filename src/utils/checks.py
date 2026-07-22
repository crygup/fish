from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from extensions.context import Context


def interaction_only():
    def predicate(ctx: Context):
        if ctx.interaction:
            return True

        raise commands.BadArgument(
            "Please use the slash command version of this command!"
        )

    return commands.check(predicate)


def lastfm_command():
    def predicate(ctx: Context):
        if ctx.author.id in ctx.bot.db_cache.lastfm:
            return True

        raise commands.BadArgument(
            "Please connect your last.fm account to the bot first before using this command. See command `link lastfm`"
        )

    return commands.check(predicate)
