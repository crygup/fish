from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from utils import BaseCog

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context


async def setup(bot: Bot):
    await bot.add_cog(Egg(bot))


class Egg(BaseCog, name="egg"):
    @commands.command(hidden=True, aliases=("ei",))
    @commands.is_owner()
    async def egg_invite(
        self,
        ctx: Context,
        uses: Optional[int] = 1,
        *,
        member: discord.Member = commands.Author,
    ):
        uses = uses or 1

        channel = self.bot.get_channel(1002262666569592903)

        if channel is None or not isinstance(channel, discord.TextChannel):
            return

        invite = await channel.create_invite(max_uses=uses)
        try:
            await member.send(f"Here is the invite: {invite.url}")
        except discord.Forbidden:
            await ctx.author.send(
                f"I was unable to DM the member, heres the invite: {invite.url}"
            )

        await ctx.message.add_reaction("\U0001f44d")
