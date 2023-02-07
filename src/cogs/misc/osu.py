from __future__ import annotations

import datetime
import re
import textwrap
from typing import TYPE_CHECKING, Union

import discord
from discord.ext import commands
from ossapi.ossapiv2 import Beatmap, Beatmapset

from utils import (
    OsuAccountConverter,
    OsuProfileView,
    human_join,
    BeatmapConverter,
    to_thread,
)

from ._base import CogBase

if TYPE_CHECKING:
    from cogs.context import Context


class Osu(CogBase):
    @commands.command(name="osu")
    async def osu_command(
        self, ctx: Context, *, user: Union[discord.User, str] = commands.Author
    ):
        account = await OsuAccountConverter().convert(ctx, user)

        country = f":flag_{account.country.code.lower()}: " if account.country else ""
        embed = discord.Embed(
            color=self.bot.embedcolor,
            title=f"{country}{account.username}",
            timestamp=account.join_date,
            url=f"https://osu.ppy.sh/users/{account.id}",
        )
        embed.set_thumbnail(url=account.avatar_url)

        footer_text = ""

        stats = account.statistics
        if stats:
            embed.add_field(name="Global Ranking", value=f"{stats.global_rank or 0:,}")
            embed.add_field(
                name="Country Ranking", value=f"{stats.country_rank or 0:,}"
            )
            embed.add_field(name="PP", value=f"{stats.pp or 0:,}")

            embed.add_field(
                name="Play time", value=str(datetime.timedelta(seconds=stats.play_time))
            )

            # fmt: off
            if any([stats.global_rank is None, stats.country_rank is None, stats.pp is None]):
                footer_text += "Note: if rank statistics are shown as 0 this could be due to no recent activity\n"
            # fmt: on

        if account.previous_usernames:
            usernames = account.previous_usernames
            if usernames:
                first_5 = usernames[:5]
                humaned_joined = human_join([f"`{u}`" for u in first_5], final="and")
                remaining = usernames[5:]
                text = f"{humaned_joined}"

                if remaining:
                    text += f"\n*({len(remaining)} remaining)*"

                embed.add_field(name="Past Usernames", value=text, inline=False)

        footer_text += f"ID: {account.id} \nJoined "
        embed.set_footer(text=footer_text)
        await ctx.send(embed=embed, view=OsuProfileView(ctx, account, embed))

    @commands.command(name="beatmap", aliases=("bm",))
    async def beatmap_command(
        self,
        ctx: Context,
        beatmap: Beatmap = commands.parameter(
            converter=BeatmapConverter, displayed_default="<beatmap url or id>"
        ),
    ):
        bms = await self.to_bms(beatmap)
        embed = discord.Embed(
            title=f"{bms.title}",
            color=self.bot.embedcolor,
            timestamp=beatmap.last_updated,
            url=beatmap.url,
        )
        embed.add_field(
            name="<:bpm:1050857111497744434> BPM",
            value=f"{beatmap.bpm or 0}",
        )
        embed.add_field(
            name="<:count_circles:1050857112793792572> Circle count",
            value=f"{beatmap.count_circles:,}",
        )
        embed.add_field(
            name="<:bpm:1050857111497744434> Slider count",
            value=f"{beatmap.count_sliders:,}",
        )
        embed.add_field(
            name="<:length:1050857116052762744> Length",
            value="00:00"
            if beatmap.total_length == 0
            else re.sub(
                r"0:(0{1,})?", "", str(datetime.timedelta(seconds=beatmap.total_length))
            ),
            inline=False,
        )

        # removed the "deleted at" field as this could mess with the alignment if it actually exists

        stats_text = f"""
        **Circle size**: {beatmap.cs}
        **HP drain**: {beatmap.drain}
        **Accuracy**: {beatmap.accuracy}
        **Approach rate**: {beatmap.ar}
        """

        max_combo = beatmap.max_combo or 0

        stats_text_2 = f"""
        **Play count**: {beatmap.playcount:,}
        **Pass count**: {beatmap.passcount:,}
        **Max combo**: {max_combo:,}
        """
        embed.add_field(name="Stats", value=textwrap.dedent(stats_text), inline=True)
        embed.add_field(name="Stats", value=textwrap.dedent(stats_text_2), inline=True)
        embed.set_footer(
            text=f"{beatmap.ranked.name.capitalize()}  - \U00002b50 {beatmap.difficulty_rating} \nLast updated "
        )
        await ctx.send(embed=embed)

        # removed beatmapset command as it doesn't really provide any more information than the beatmap command, it would be pointless
