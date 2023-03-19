from __future__ import annotations
import datetime
import textwrap

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .functions import *
from utils import LastfmConverter, get_lastfm, BlankException, UPVOTE, DOWNVOTE, BaseCog

if TYPE_CHECKING:
    from cogs.context import Context


class FmCommand(BaseCog):
    @commands.group(
        name="fm", aliases=("nowplaying", "np"), invoke_without_command=True
    )
    async def last_fm(self, ctx: Context, username: LastfmConverter = commands.Author):
        """Displays your last scrobble from last.fm"""
        name = (
            await get_lastfm(ctx.bot, ctx.author.id)
            if username == ctx.author
            else str(username)
        )

        try:
            track = (
                await self.bot.lastfm.fetch_user_recent_tracks(
                    user=name, extended=True, limit=1
                )
            )[0]
        except IndexError:
            raise BlankException(f"{name} has no recent tracks.")

        description = f"""
        **Artist**: {discord.utils.escape_markdown(track.artist.name)}
        **Track**: {discord.utils.escape_markdown(track.name)}
        **Album**: {discord.utils.escape_markdown(track.album.name)}
        """

        loved = f"\U00002764\U0000fe0f " if track.loved else ""
        footer_text = f"{loved}{name} has {track.attr.total_scrobbles:,} scrobbles"

        if track.played_at:
            footer_text += "\nLast scrobble"

        embed = discord.Embed(
            color=self.bot.embedcolor,
            description=textwrap.dedent(description),
            timestamp=track.played_at.replace(tzinfo=datetime.timezone.utc)
            if track.played_at
            else None,
        )

        embed.set_author(
            name=f"{'Now playing -' if track.now_playing else 'Last track for - '} {discord.utils.escape_markdown(name)}",
            url=track.url,
            icon_url=ctx.author.display_avatar.url,
        )

        embed.set_thumbnail(url=track.images.extra_large or track.images.large)
        embed.set_footer(text=footer_text)
        message = await ctx.send(embed=embed, check_ref=True)

        if str(ctx.author.id) in await self.bot.redis.smembers("fm_autoreactions"):
            emojis = [UPVOTE, DOWNVOTE]
            for emoji in emojis:
                try:
                    await message.add_reaction(emoji)
                except:
                    continue

    @last_fm.command(name="set")
    async def lastfm_set(self, ctx: Context, username: str):
        """Alias for set lastfm command"""

        command = self.bot.get_command("set lastfm")

        if command is None:
            return

        await command(ctx, username=username)
