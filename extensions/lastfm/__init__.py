from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

import discord
from dateutil.parser import parse
from discord.ext import commands

from core import Cog

if TYPE_CHECKING:
    from context import Context
    from lastfm import Track

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
    async def fm(self, ctx: Context, username: str = "crygup"):
        async with ctx.typing():
            user = await ctx.bot.fm.get_user_info(username)
            track: Track = (await user.get_recent_tracks(extended=True))[0]

            embed = discord.Embed(color=0xFAA0C1)

            np = track.is_now_playing()
            embed.set_author(
                name=f"{'Now playing' if np else 'Last track for'} - {username}",
                icon_url=ctx.author.display_avatar.url,
                url=f"https://last.fm/user/{username}",
            )

            try:
                embed.set_thumbnail(url=track._data["image"][-1]["#text"])
            except (KeyError, IndexError):
                pass

            emoji = "\U00002764\U0000fe0f"

            reformed_url = re.sub(
                r"https://www.last.fm/",
                f"https://www.last.fm/user/{username}/library/",
                track.url,
            )
            album_text = track.name if track.album is None else track.album.name

            assert track.artist
            loved = True if track._data["loved"] == "1" else None
            description = f"""
            [{track.name}]({reformed_url}) {emoji if loved else ''}
            By **{track.artist.name}** on *{album_text}*
            """

            if np is False:
                embed.set_footer(text="Scrobbled")
                embed.timestamp = parse(track._data["date"]["#text"])

            embed.description = textwrap.dedent(description)

        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: Fishie):
    await bot.add_cog(LastFM())
