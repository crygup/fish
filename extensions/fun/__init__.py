from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING

import discord
import psutil
from discord.ext import commands

from core import Cog
from utils import to_image

from .helpers import RPSView, WTPView, dagpi
from .about import About

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Fun(About):
    """Fun miscellaneous commands"""

    emoji = discord.PartialEmoji(name="\U0001f604")

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot
        self.process = psutil.Process()
        self.invite_url = discord.utils.oauth_url(
            self.bot.config["ids"]["bot_id"], permissions=self.bot.bot_permissions
        )

    @commands.command(name="rock-paper-scissors", aliases=("rockpaperscissors", "rps"))
    async def RPSCommand(self, ctx: Context):
        """
        Play rock paper scissors against me!
        """
        await ctx.send(view=RPSView(ctx))

    @commands.command(name="monark")
    @commands.cooldown(1, 5)
    async def monark(self, ctx: Context):
        """monark said this"""

        await ctx.send(
            file=discord.File(
                rf"files/monark/monark{random.randint(1,3)}.png", "monark.png"
            )
        )

    @commands.command(name="merica", aliases=("cm",))
    @commands.cooldown(1, 5)
    async def merica(self, ctx: Context, *, text: str):
        """we love america!!!"""

        await ctx.send(
            re.sub(" ", " \U0001f1fa\U0001f1f8 ", text)[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="invite", aliases=("join",))
    async def invite(self, ctx: Context):
        """Sends a link to add me to a server."""

        await ctx.send(self.invite_url)

    @commands.command(name="wtp", hidden=True)
    async def wtp(self, ctx: Context):
        await ctx.typing()

        data = await dagpi(self.bot, ctx.message, "https://api.dagpi.xyz/data/wtp")

        embed = discord.Embed(color=self.bot.embedcolor)
        embed.set_author(name="Who's that pokemon?")

        image = await to_image(ctx.session, data["question"])
        file = discord.File(fp=image, filename="pokemon.png")

        embed.set_image(url="attachment://pokemon.png")

        await ctx.send(embed=embed, file=file, view=WTPView(ctx, data))


async def setup(bot: Fishie):
    await bot.add_cog(Fun(bot))
