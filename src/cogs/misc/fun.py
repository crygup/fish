from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from utils import human_join, to_bytesio, RPSView, WTPView

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Fun(CogBase):
    @commands.command(name="character")
    async def character(self, ctx: Context, *, character: str):
        """Gets the information about a genshin character."""

        pattern = re.compile(r'"(?P<name>[a-zA-Z-]{1,})"')
        async with self.bot.session.get("https://api.genshin.dev/characters") as r:
            results = await r.text()

        characters = pattern.findall(results)

        if not character.lower() in [c for c in characters]:

            message = "Character not found.\n\n"
            maybe = difflib.get_close_matches(character.lower(), characters)
            if maybe:
                message += f"Did you mean `{human_join(maybe)}`?"

            await ctx.send(message)
            return

        async with self.bot.session.get(
            f"https://api.genshin.dev/characters/{character}"
        ) as r:
            results = await r.json()

        embed = discord.Embed(
            color=ctx.bot.embedcolor, description=results["description"]
        )
        icon_fp = await to_bytesio(
            ctx.session, f"https://api.genshin.dev/characters/{character}/icon"
        )
        icon_file = discord.File(icon_fp, filename=f"{character}.png")

        embed.set_author(
            name=f"{results['name']}  •  {results['rarity']} \U00002b50",
            icon_url=f"attachment://{character}.png",
        )
        embed.set_thumbnail(url=f"attachment://{character}.png")

        embed.add_field(name="Weapon", value=results["weapon"])
        embed.add_field(name="Vision", value=results["vision"])
        embed.add_field(name="Nation", value=results["nation"])
        embed.add_field(name="Affiliation", value=results["affiliation"])
        embed.add_field(name="Constellation", value=results["constellation"])
        embed.add_field(name="Birthday", value=results["birthday"])

        await ctx.send(embed=embed, file=icon_file)

    @commands.command(name="monark", hidden=True)
    @commands.cooldown(1, 5)
    async def monark(self, ctx: Context):
        """monark said this"""
        await ctx.send(
            "https://cdn.discordapp.com/attachments/884188416835723285/1006540930448375919/IMG_0886.jpg"
        )

    @commands.command(name="merica", hidden=True)
    @commands.cooldown(1, 5)
    async def merica(self, ctx: Context, *, text: str):
        """we love america!!!"""

        await ctx.send(
            re.sub(" ", " \U0001f1fa\U0001f1f8 ", text)[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="rock-paper-scissors", aliases=("rockpaperscissors", "rps"))
    async def RPSCommand(self, ctx: Context):
        await ctx.send(view=RPSView(ctx))

    @commands.command(name="wtp", hidden=True)
    async def wtp(self, ctx: Context):
        await ctx.trigger_typing()
        data = await ctx.dagpi("https://api.dagpi.xyz/data/wtp")
        embed = discord.Embed(color=self.bot.embedcolor)
        embed.set_author(name="Who's that pokemon?")
        image = await to_bytesio(ctx.session, data["question"])
        file = discord.File(fp=image, filename="pokemon.png")
        embed.set_image(url="attachment://pokemon.png")
        await ctx.send(embed=embed, file=file, view=WTPView(ctx, data))
