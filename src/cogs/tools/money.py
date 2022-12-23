from __future__ import annotations

import re
from typing import TYPE_CHECKING

from discord.ext import commands

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class MoneyConveter(commands.Converter):
    async def convert(self, _, argument: str):
        money = re.sub(r"(_|,)", "", argument)
        money = re.sub(r"(k{1,})$", "000", money)

        if money.isdigit():
            return int(money)

        raise ValueError(f"Converting to integer failed. \n`{money}`")


class MoneyCommands(CogBase):
    @commands.command(name="money", hidden=True)
    async def money(
        self,
        ctx: Context,
        money: int = commands.parameter(
            displayed_default="<amount>", converter=MoneyConveter
        ),
    ):
        await ctx.send(f"{money:,}")
