from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

import discord
from discord.abc import Messageable
from discord.ext import commands

from core import Cog
from utils import fish_owner, greenTick

if TYPE_CHECKING:
    from core import Fishie


class Owner(Cog):
    emoji = fish_owner
    hidden: bool = True

    @commands.command(name="reply")
    async def reply(
        self,
        ctx: commands.Context[Fishie],
        message: Union[str, int],
        channel: Optional[Messageable] = None,
        *,
        text: str,
    ):
        """Reply to a message"""
        _message = await ctx.bot.fetch_message(message=message, channel=channel)

        await _message.reply(text)

        await ctx.message.add_reaction(greenTick)

    @commands.command(name="message", aliases=("send", "msg", "dm"))
    async def message(
        self,
        ctx: commands.Context[Fishie],
        channel: Optional[Union[Messageable, discord.User]] = commands.CurrentChannel,
        *,
        text: str,
    ):
        """Send a message"""
        channel = channel or ctx.channel
        await channel.send(text)

        await ctx.message.add_reaction(greenTick)

    async def cog_check(self, ctx: commands.Context[Fishie]) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True

        raise commands.BadArgument("You are not allowed to use this command.")


async def setup(bot: Fishie):
    await bot.add_cog(Owner(bot))
