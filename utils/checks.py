from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


def interaction_only():
    def predicate(ctx: Context):
        if ctx.interaction:
            return True

        raise commands.BadArgument(
            "Please use the slash command version of this command!"
        )

    return commands.check(predicate)
