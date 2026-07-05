from __future__ import annotations

from typing import TYPE_CHECKING

import discord
import re
import datetime
from discord.ext import commands
from discord import app_commands

from core import Cog
from utils import (
    lastfm_command,
    to_image,
    format_millis,
    plural,
    fish_discord,
    lfm_emoji,
)
from typing import Dict, Any
from .top import Top
from .charts import Charts

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


class Lastfm(Top, Charts):
    """Last.fm integration"""

    emoji = lfm_emoji

    def __init__(self, bot: Fishie):
        super().__init__()
        self.bot = bot

    @commands.hybrid_command(
        name="fm", enabled=True, aliases=("np", "nowplaying", "fuckyoutony")
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def justfmmealreadybruh(
        self, ctx: Context, user: discord.User = commands.Author
    ):
        """Get your currently playing or most recently listened to song from last.fm"""
        async with ctx.typing():
            try:
                lfm_user = self.bot.db_cache.lastfm[user.id]
            except KeyError:
                raise commands.BadArgument(
                    "This user has not connected their last.fm account"
                )

            data = {"method": "user.getrecenttracks", "user": lfm_user}

            response = await self.bot.lfm_get(data)
            tracks = response.get("recenttracks", {}).get("track")
            if not tracks:
                raise commands.BadArgument(
                    f"No recent tracks found for **{lfm_user}**."
                )
            lt = tracks[0] if isinstance(tracks, list) else tracks

            files = []
            embed = discord.Embed(color=self.bot.embedcolor)
            author_name = "was listening to" if lt.get("date") else "is listening to"

            thumbnail_url = re.sub(r"/u/.*/", "/u/", lt["image"][-1]["#text"])

            if not re.search("2a96cbd8b46e442fc41c2b86b821562f.png", thumbnail_url):
                fp = await to_image(ctx.session, thumbnail_url)
                file = discord.File(fp=fp, filename="cover.png")
                files.append(file)
                embed.set_thumbnail(url="attachment://cover.png")

            embed.set_author(
                name=f"{user.display_name} {author_name}"[:256],
                icon_url=user.display_avatar.url,
                url=f"https://last.fm/user/{lfm_user}",
            )

            embed.url = lt["url"]
            embed.description = (
                f"**{lt['artist']['#text']}** - *{lt['album']['#text']}*"
            )

            tData = (
                {"method": "track.getInfo", "mbid": lt["mbid"], "user": lfm_user}
                if lt.get("mbid")
                else {
                    "method": "track.getInfo",
                    "artist": lt["artist"]["#text"],
                    "track": lt["name"],
                    "user": lfm_user,
                }
            )

            try:
                tResponse = await self.bot.lfm_get(tData)
                t = tResponse.get("track")
            except Exception:
                t = None

            footer_text = ""
            if t:
                tp = int(t.get("userplaycount", 0))
                dur = format_millis(int(t.get("duration", 0)))
                if tp != 0:
                    splitter = " - " if dur != "0" else ""
                    footer_text += f"{tp:,} track {plural(tp, False):play} {splitter}"
                if dur != "0":
                    footer_text += f"\U0001f551 {dur}"

            if lt.get("date"):
                embed.timestamp = datetime.datetime.fromtimestamp(
                    int(lt["date"]["uts"])
                )
                footer_text += "\nLast play"

            if footer_text:
                embed.set_footer(text=footer_text)

            loved = f" \U00002764\U0000fe0f" if t and t.get("userloved") != "0" else ""
            embed.title = f'{lt["name"]}{loved}'

            await ctx.send(embed=embed, files=files)


async def setup(bot: Fishie):
    await bot.add_cog(Lastfm(bot))
