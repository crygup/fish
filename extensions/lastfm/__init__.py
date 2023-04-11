from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

import discord
from dateutil.parser import parse
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from core import Fishie


class LastFM(Cog):
    """Commands related to the site [last.fm](https://last.fm)"""

    emoji = discord.PartialEmoji(name="lastfm", id=1092597325798588550)

    @commands.hybrid_command(
        name="fm",
        aliases=(
            "np",
            "nowplaying",
        ),
    )
    async def fm(self, ctx: commands.Context[Fishie]):
        async with ctx.typing():
            username = "crygup"
            async with ctx.bot.session.get(
                "http://ws.audioscrobbler.com/2.0",
                params={
                    "method": "user.getrecenttracks",
                    "user": username,
                    "format": "json",
                    "api_key": ctx.bot.config["keys"]["lastfm"],
                    "extended": "1",
                },
            ) as resp:
                try:
                    track = (await resp.json())["recenttracks"]["track"][0]
                except (KeyError, IndexError):
                    return await ctx.send(
                        "Could not find any recent tracks for this user."
                    )

                embed = discord.Embed(color=0xFAA0C1)

                np = bool(track.get("@attr", False))
                embed.set_author(
                    name=f"{'Now playing' if np else 'Last track for'} - {username}",
                    icon_url=ctx.author.display_avatar.url,
                    url=f"https://last.fm/user/{username}",
                )

                try:
                    embed.set_thumbnail(url=track["image"][-1]["#text"])
                except (KeyError, IndexError):
                    pass

                album_text = track["name"]

                try:
                    album_text = track["album"]["#text"]
                except (KeyError, IndexError):
                    pass

                emoji = "\U00002764\U0000fe0f"

                reformed_url = re.sub(
                    r"https://www.last.fm/",
                    f"https://www.last.fm/user/{username}/library/",
                    track["url"],
                )

                description = f"""
                [{track['name']}]({reformed_url}) {emoji if track['loved'] == '1' else ''}
                By **{track['artist']['name']}** on *{album_text}*
                """

                embed.description = textwrap.dedent(description)

                if np is False:
                    embed.set_footer(text="Scrobbled")
                    embed.timestamp = parse(track["date"]["#text"])

            await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: Fishie):
    await bot.add_cog(LastFM())
