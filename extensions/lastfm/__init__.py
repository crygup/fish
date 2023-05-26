from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING, Optional

import discord
from dateutil.parser import parse
from discord.ext import commands

from core import Cog
from utils import LastfmAccountConverter
from lastfm.errors import HTTPException
if TYPE_CHECKING:
    from context import Context
    from lastfm import Track

    from core import Fishie


class LastFM(Cog):
    """Commands related to the site [last.fm](https://last.fm)"""

    emoji = discord.PartialEmoji(name="lastfm", id=1092597325798588550)

    @commands.group(
        name="fm",
        aliases=(
            "np",
            "nowplaying",
        ),
        invoke_without_command=True,
    )
    async def fm(self, ctx: Context, username: Optional[str] = None):
        username = await LastfmAccountConverter().convert(
            ctx, username or str(ctx.author.id)
        )
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

        await ctx.send(embed=embed)

    @fm.command(name="link", aliases=("set",))
    async def fm_link(self, ctx: Context, username: str):
        if not re.match("[a-zA-Z0-9_-]{2,15}", username):
            raise commands.BadArgument("Username provided is not valid.")

        try:
            await ctx.bot.fm.get_user_info(username)
        except HTTPException:
            raise commands.BadArgument(
                "The account you provided doesn't seem to exist, please double check your spelling."
            )

        sql = f"""
        INSERT INTO accounts (last_fm, user_id) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE
        SET last_fm = $1 WHERE accounts.user_id = $2
        """
        await ctx.bot.pool.execute(sql, username, ctx.author.id)
        await ctx.bot.redis.set(f"fm:{ctx.author.id}", username)
        await ctx.send(str(ctx.bot.custom_emojis.greenTick))

    @fm.command(name="unlink", aliases=("imset",))
    async def fm_unlink(self, ctx: Context):
        sql = f"""
        UPDATE accounts SET last_fm = NULL WHERE accounts.user_id = $1 RETURNING *
        """

        await ctx.bot.pool.execute(sql, ctx.author.id)
        await ctx.bot.redis.delete(f"fm:{ctx.author.id}")
        await ctx.send(str(ctx.bot.custom_emojis.greenTick))

async def setup(bot: Fishie):
    await bot.add_cog(LastFM())
