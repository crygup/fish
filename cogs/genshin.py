import difflib
import re
from typing import Union

import discord
from bot import Bot, Context
from discord.ext import commands
from utils import get_genshin, human_join

import genshin


async def setup(bot: Bot):
    await bot.add_cog(Genshin(bot))


class Genshin(commands.Cog, name="genshin"):
    """Genshin related commands."""

    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(name="character")
    async def character(self, ctx: Context, *, character: str):
        """Gets the information about a character."""

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
        icon_fp = await ctx.to_image(
            f"https://api.genshin.dev/characters/{character}/icon"
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
