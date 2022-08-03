from __future__ import annotations

from typing import Union

import discord
from discord.ext import commands
from ossapi.ossapiv2 import UserIdT
from utils import BeatmapConverter, BeatmapSetConverter, get_osu, UnknownAccount

from bot import Bot, Context


async def setup(bot: Bot):
    await bot.add_cog(Osu(bot))


class Osu(commands.Cog, name="osu"):
    """osu.ppy.sh commands"""

    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command(name="osu", invoke_without_command=True)
    async def osu_command(
        self,
        ctx: Context,
        account: Union[int, discord.User, str, UserIdT] = commands.Author,
    ):
        """Gets stats for an osu! account.

        You can either give an accuont ID or a username."""
        if isinstance(account, (discord.User, discord.Member)):
            account = await get_osu(ctx.bot, account.id)

        try:
            account = self.bot.osu.user(account)
        except ValueError:
            raise UnknownAccount("Unknown account.")

        e = discord.Embed(
            url=f"https://osu.ppy.sh/users/{account.id}",
            color=self.bot.embedcolor,
            timestamp=account.join_date,
        )
        e.set_author(name=account.username, icon_url=account.avatar_url)
        e.set_thumbnail(url=account.avatar_url)
        e.set_footer(text=f"ID: {account.id} \nCreated at")

        followers = f"{(account.follower_count):,}"
        e.add_field(name="Name", value=account.username)
        e.add_field(
            name="Country",
            value=account.country.name.capitalize() if account.country else "Unknown",
        )
        e.add_field(name="Followers", value=followers)

        stats = account.statistics
        if stats is not None:
            if stats.global_rank:
                e.add_field(name="Rank", value=f"{stats.global_rank:,}")
                e.add_field(name="Country Rank", value=f"{stats.country_rank:,}")
            e.add_field(name="PP", value=f"{stats.pp:,}")
            e.add_field(name="Accuracy", value=f"{stats.hit_accuracy:.2f}%")
            e.add_field(name="Level", value=f"{stats.level.current:,}")
            e.add_field(name="Play Count", value=f"{stats.play_count:,}")

        await ctx.send(embed=e, check_ref=True)

    @commands.command(name="beatmap", aliases=["bm"])
    async def osu_beatmap(self, ctx: Context, beatmap_id: BeatmapConverter):
        """Gets info about a beatmap.

        You can either give a beatmap ID or a beatmap link."""

        try:
            bm = self.bot.osu.beatmap(beatmap_id)  # type: ignore
        except ValueError:
            raise UnknownAccount("Unknown beatmap.")

        bm_set = bm.beatmapset()
        ranked = bm_set.ranked_date
        submitted = (
            discord.utils.format_dt(bm_set.submitted_date, "R")
            if bm_set.submitted_date
            else "Not submitted"
        )
        e = discord.Embed(
            url=bm.url,
            color=self.bot.embedcolor,
            timestamp=ranked,
        )
        e.set_author(name=f"{bm.version}/{bm_set.title}")
        e.add_field(
            name="Creator",
            value=f'[{bm_set.creator}](https://osu.ppy.sh/users/{bm_set.creator} "Link to profile")',
        )
        e.add_field(name="Artist", value=bm_set.artist)
        e.add_field(name="Beatmap set ID", value=bm_set.id)
        e.add_field(name="Submitted", value=submitted)
        e.add_field(name="Status", value=bm.ranked.name.title())
        e.add_field(name="Playcount", value=f"{bm.playcount:,}")
        e.add_field(
            name="Objects",
            value=f"**Circles**: {bm.count_circles:,}\n"
            f"**Sliders**: {bm.count_sliders:,}\n"
            f"**Spinners**: {bm.count_spinners:,}",
        )
        bpm = f"{bm.bpm:,}" if bm.bpm else "Not set"
        e.add_field(
            name="Difficulty",
            value=f"**BPM**: {bpm}\n" f"**CS**: {bm.cs:,}\n" f"**AR**: {bm.ar:,}\n",
        )
        max_combo = f"{bm.max_combo:,}" if bm.max_combo else "Not set"
        e.add_field(name="Max combo", value=max_combo)
        e.set_footer(text=f"ID: {bm.id} \n{'Ranked at' if ranked else 'Not ranked'}")
        await ctx.send(embed=e, check_ref=True)

    @commands.command(name="beatmapset", aliases=["bms"])
    async def osu_beatmapset(self, ctx: Context, beatmap_id: BeatmapSetConverter):
        """Gets info about a beatmap set.

        You can either give a beatmap ID or a beatmap link."""

        try:
            bm = self.bot.osu.beatmapset(beatmap_id)  # type: ignore
        except ValueError:
            raise UnknownAccount("Unknown beatmap set.")

        ranked = bm.ranked_date
        e = discord.Embed(
            url=f"https://osu.ppy.sh/beatmapsets/{bm.id}",
            color=self.bot.embedcolor,
            timestamp=bm.ranked_date,
        )
        e.set_author(name=bm.title)
        e.add_field(
            name="Creator",
            value=f'[{bm.creator}](https://osu.ppy.sh/users/{bm.creator} "Link to profile")',
        )
        e.add_field(name="Artist", value=bm.artist)
        e.add_field(name="Playcount", value=f"{bm.play_count:,}")
        e.add_field(name="Status", value=f"{bm.ranked.name.title()}")
        e.add_field(
            name="Beatmaps", value=len(bm.beatmaps) if bm.beatmaps else "No beatmaps"
        )
        e.add_field(name="Favorites", value=f"{bm.favourite_count:,}")
        e.add_field(
            name="Submitted",
            value=discord.utils.format_dt(bm.submitted_date, "R")
            if bm.submitted_date
            else "Not submitted",
        )
        e.set_footer(text=f"ID: {bm.id} \n{'Ranked at' if ranked else 'Not ranked'}")
        await ctx.send(embed=e, check_ref=True)
