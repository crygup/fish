from __future__ import annotations

from io import BytesIO
import re
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Literal, TypeAlias, Union

import discord
from discord.ext import commands
from discord import MediaGalleryItem, Optional, app_commands
from discord import ui
from core import Cog
from utils import (
    lastfm_command,
    LastfmTimeConverter,
    lastfm_period,
    to_image,
    format_bytes,
    AuthorView,
    response_checker
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


class ChartEmbed(ui.LayoutView):
    def __init__(
        self,
        ctx: Context,
        image: discord.File,
        user: discord.User,
        mode: topMode,
        lfm_user: str,
        total_plays: int,
        time_period: str = "overall",
        count: int = 9,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.text = f"### [{user.display_name}'s](https://www.last.fm/user/{lfm_user}) {modeName[mode]} chart, {lastfm_period[time_period]}."
        self.text2 = f"-# {lfm_user} has {total_plays:,} scrobbles"
        self.mode = mode
        self.count = count
        self.time_period = time_period
        self.image = ui.MediaGallery(MediaGalleryItem(image))
        self.container = ui.Container(
            self.image,
            ui.TextDisplay(self.text),
            ui.TextDisplay(self.text2),
            accent_color=self.ctx.bot.embedcolor,
        )
        self.add_item(self.container)


async def lfm_get(ctx: Context, data: Dict[Any, Any]):
    async with ctx.bot.session.get(ctx.bot.lfm_api, params=data) as resp:
        return await resp.json()


async def chart_cmd(
    ctx: Context,
    user: discord.User,
    mode: topMode,
    count: int = 9,
    time_period: str = "overall",
):
    try:
        lfm_user = ctx.bot.db_cache.lastfm[user.id]
    except KeyError:
        raise commands.BadArgument("This user has not connected their last.fm account")

    file = await make_image(ctx, user, mode, count, time_period=time_period)

    data = {"method": "user.getrecenttracks", "user": lfm_user}

    rtResponse = (await lfm_get(ctx, data))[f"recenttracks"]["@attr"]
    view = ChartEmbed(
        ctx,
        file,
        user,
        mode,
        lfm_user,
        int(rtResponse["total"]),
        time_period=time_period,
        count=count,
    )
    await ctx.send(view=view, file=file)


async def search_spotify(
    ctx: Context,
    mode: str,
    query: str,
) -> str:
    url = "https://api.spotify.com/v1/search"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ctx.bot.spotify_key}",
    }

    api_data = {"q": query, "type": mode, "limit": "10", "market": "US"}

    async with ctx.session.get(url, headers=headers, params=api_data) as resp:
        response_checker(resp)
        data = await resp.json()

        return data[f"{mode}s"]['items'][0]['images'][0]['url']


async def make_image(
    ctx: Context,
    user: discord.User,
    mode: topMode,
    count: int = 9,
    time_period: str = "overall",
):
    try:
        lfm_user = ctx.bot.db_cache.lastfm[user.id]
    except KeyError:
        raise commands.BadArgument("This user has not connected their last.fm account")

    data = {
        "method": f"user.{mode}",
        "user": lfm_user,
        "limit": 200,
        "period": time_period,
    }

    cv = 0
    images = []
    response = (await lfm_get(ctx, data))[f"top{modeName[mode]}s"]
    items: List[Dict[Any, Any]] = response[modeName[mode]]
    bio = ""
    for item in items:
        image_url = re.sub(r"/u/.*/", "/u/", item["image"][-1]["#text"])

        if re.search("2a96cbd8b46e442fc41c2b86b821562f.png", image_url):
            search = f"{item['name']}"

            if item.get('artist'):
                search += f"artist:{item['artist']}"

            spotify = await search_spotify(ctx, modeName[mode], search)
            image_url = spotify

        name = (
            f"{item['name']} by {item['artist']['name']}"
            if item.get("artist")
            else item["name"]
        )
        bio += f"#{cv+1} {name} "

        fp = await to_image(ctx.session, image_url, bytes=True)

        cv += 1
        images.append(fp)
        if cv >= count:
            break

    fp = await format_bytes(
        filesize_limit=ctx.guild.filesize_limit if ctx.guild else 10485760,
        images=images,
    )
    file = discord.File(fp, "chart.png", description=bio[:1024])

    return file


class Charts(Cog):
    @commands.hybrid_group(name="chart", fallback="albums", aliases=("c",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def chart(
        self,
        ctx: Context,
        count: int = 9,
        time_period: str = period,
        *,
        user: discord.User = commands.Author,
    ):
        """Displays a grid view of your top albums"""

        async with ctx.typing():
            n = re.search(r"^[1-9][0-9]?$|^100$", str(count))
            n = int(n.group(0)) if n else 9

            await chart_cmd(ctx, user, "gettopalbums", time_period=time_period, count=n)

    @chart.command(name="artists", aliases=("artist", "a"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def chart_artists(
        self,
        ctx: Context,
        count: int = 9,
        time_period: str = period,
        *,
        user: discord.User = commands.Author,
    ):
        """Displays a grid view of your top artists"""

        async with ctx.typing():
            n = re.search(r"^[1-9][0-9]?$|^100$", str(count))
            n = int(n.group(0)) if n else 9

            await chart_cmd(
                ctx, user, "gettopartists", time_period=time_period, count=n
            )

    @chart.command(name="tracks", aliases=("track", "t"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def chart_tracks(
        self,
        ctx: Context,
        count: int = 9,
        time_period: str = period,
        *,
        user: discord.User = commands.Author,
    ):
        """Displays a grid view of your top tracks"""

        async with ctx.typing():
            n = re.search(r"^[1-9][0-9]?$|^100$", str(count))
            n = int(n.group(0)) if n else 9

            await chart_cmd(ctx, user, "gettoptracks", time_period=time_period, count=n)
