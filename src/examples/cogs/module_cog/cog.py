from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Example(CogBase):
    @commands.command(name="example")
    async def example_command(self, ctx: Context):
        await ctx.send("Example!")
