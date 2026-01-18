from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Literal, TypeAlias, Union

import discord
from discord.ext import commands
from discord import app_commands

from core import Cog
from utils import (
    lastfm_command,
    LastfmTimeConverter,
    interaction_only,
    SimplePages,
    plural,
    lastfm_period,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context
    from .__init__ import Lastfm

period: str = commands.param(converter=LastfmTimeConverter, default="overall")
topMode: TypeAlias = Union[
    Literal["gettopartists"], Literal["gettopalbums"], Literal["gettoptracks"]
]
modeName = {"gettopartists": "artist", "gettopalbums": "album", "gettoptracks": "track"}


class Top(Cog):
    async def lfm_get(self, data: Dict[Any, Any]):
        async with self.bot.session.get(self.bot.lfm_api, params=data) as resp:
            return await resp.json()

    async def list_top(
        self,
        ctx: Context,
        user: discord.User,
        mode: topMode,
        time_period: str = "overall",
    ):
        try:
            lfm_user = self.bot.db_cache.lastfm[user.id]
        except KeyError:
            raise commands.BadArgument(
                "This user has not connected their last.fm account"
            )

        data = {
            "method": f"user.{mode}",
            "user": lfm_user,
            "limit": 100,
            "period": time_period,
        }

        response = (await self.lfm_get(data))[f"top{modeName[mode]}s"]
        items = response[modeName[mode]]

        info = [
            f"**[{a['name']}]({a['url']})** - *{int(a['playcount']):,} {plural(int(a['playcount']), False):play}*"
            for a in items
        ]

        pages = SimplePages(entries=info, per_page=10, ctx=ctx)
        pages.embed.title = (
            f"{user.display_name}'s top {lastfm_period[time_period]} {modeName[mode]}s"
        )
        pages.embed.url = f"https://www.last.fm/user/{lfm_user}"
        pages.embed.color = self.bot.embedcolor
        await pages.start(ctx)

    @commands.hybrid_group(name="top", fallback="artists")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @interaction_only()
    @lastfm_command()
    async def top_group(
        self,
        ctx: Context,
        time_period: str = period,
        user: discord.User = commands.Author,
    ):
        """Show your top artists"""
        async with ctx.typing():
            await self.list_top(ctx, user, "gettopartists", time_period)

    @top_group.command(name="albums")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @interaction_only()
    @lastfm_command()
    async def top_albums_sc(
        self,
        ctx: Context,
        time_period: str = period,
        user: discord.User = commands.Author,
    ):
        """Show your top albums"""
        async with ctx.typing():
            await self.list_top(ctx, user, "gettopalbums", time_period)

    @top_group.command(name="tracks")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @interaction_only()
    @lastfm_command()
    async def top_tracks_sc(
        self,
        ctx: Context,
        time_period: str = period,
        user: discord.User = commands.Author,
    ):
        """Show your top tracks"""
        async with ctx.typing():
            await self.list_top(ctx, user, "gettoptracks", time_period)

    @commands.command(name="topartists", aliases=("ta",))
    @lastfm_command()
    async def _top_artists(
        self,
        ctx: Context,
        time_period: str = period,
        user: discord.User = commands.Author,
    ):
        """Show your top artists"""
        async with ctx.typing():
            await self.list_top(ctx, user, "gettopartists", time_period)

    @commands.command(name="topalbums", aliases=("tab",))
    @lastfm_command()
    async def _top_albums(
        self,
        ctx: Context,
        time_period: str = period,
        user: discord.User = commands.Author,
    ):
        """Show your top albums"""
        async with ctx.typing():
            await self.list_top(ctx, user, "gettopalbums", time_period)

    @commands.command(name="toptracks", aliases=("tt",))
    @lastfm_command()
    async def _top_tracks(
        self,
        ctx: Context,
        time_period: str = period,
        user: discord.User = commands.Author,
    ):
        """Show your top tracks"""
        async with ctx.typing():
            await self.list_top(ctx, user, "gettoptracks", time_period)
