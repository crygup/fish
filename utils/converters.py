from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from extensions.context import Context


class LastfmAccountConverter(commands.Converter[str]):
    async def convert(self, ctx: Context, argument: str) -> str:  # type: ignore
        argument = re.sub(r"^fm:", "", argument)

        try:
            user = await commands.UserConverter().convert(ctx, argument)
        except (commands.BadArgument, commands.CommandError):
            user = None

        if isinstance(user, discord.User):
            account = await ctx.bot.redis.get(f"fm:{user.id}")
            if account is None:
                prefix = await ctx.bot.get_prefix(ctx.message)
                raise commands.BadArgument(
                    f"{['User does', 'You do'][ctx.author.id == user.id]} not have a last.fm account set, {['they', 'you'][ctx.author.id == user.id]} can set one with `{prefix[-1]}fm link <username>`"
                )
            return str(account)

        return argument
