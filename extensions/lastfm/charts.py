from __future__ import annotations

import asyncio
from io import BytesIO
import re
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Literal, TypeAlias, Union

import discord
from discord.ext import commands
from discord import MediaGalleryItem, app_commands
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

async def chart_cmd(
    ctx: Context,
    user: discord.User,
    mode: topMode,
    count: int = 9,
    time_period: str = "overall",
    *,
    xbound: int = 0,
    ybound: int = 0,
):
    try:
        lfm_user = ctx.bot.db_cache.lastfm[user.id]
    except KeyError:
        raise commands.BadArgument("This user has not connected their last.fm account")

    file = await make_image(ctx, lfm_user, mode, count, time_period=time_period, xbound=xbound, ybound=ybound)

    data = {"method": "user.getrecenttracks", "user": lfm_user}
    response = await ctx.bot.lfm_get(data)
    attr = response.get("recenttracks", {}).get("@attr", {})
    total = int(attr.get("total", 0))
    view = ChartEmbed(
        ctx,
        file,
        user,
        mode,
        lfm_user,
        total,
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

        items = data.get(f"{mode}s", {}).get("items", [])
        if not items:
            raise commands.BadArgument(f"No Spotify results found for `{query}`.")
        images = items[0].get("images", [])
        if not images:
            raise commands.BadArgument(f"No cover image found on Spotify for `{query}`.")
        return images[0]["url"]


async def make_image(
    ctx: Context,
    lfm_user: str,
    mode: topMode,
    count: int = 9,
    time_period: str = "overall",
    *,
    xbound: int = 0,
    ybound: int = 0,
):
    data = {
        "method": f"user.{mode}",
        "user": lfm_user,
        "limit": 200,
        "period": time_period,
    }
    response = (await ctx.bot.lfm_get(data))[f"top{modeName[mode]}s"]
    items: List[Dict[Any, Any]] = response[modeName[mode]][:count]

    # Phase 1: collect items and resolve Spotify for missing covers in parallel
    bios: list[str] = []
    image_urls: list[str] = []
    spotify_tasks: list[tuple[int, str, str]] = []  # (index, mode, query)

    for i, item in enumerate(items):
        url = re.sub(r"/u/.*/", "/u/", item["image"][-1]["#text"])
        if re.search("2a96cbd8b46e442fc41c2b86b821562f.png", url):
            query = item["name"]
            if item.get("artist"):
                query += f" artist:{item['artist']}"
            spotify_tasks.append((i, modeName[mode], query))
        image_urls.append(url)

        name = (
            f"{item['name']} by {item['artist']['name']}"
            if item.get("artist")
            else item["name"]
        )
        bios.append(f"#{i + 1} {name}")

    if spotify_tasks:
        async def _resolve_spotify(idx: int, s_mode: str, query: str) -> tuple[int, str]:
            try:
                return idx, await search_spotify(ctx, s_mode, query)
            except Exception:
                return idx, image_urls[idx]

        results = await asyncio.gather(*(_resolve_spotify(*t) for t in spotify_tasks))
        for idx, url in results:
            image_urls[idx] = url

    # Phase 2: download all images in parallel
    async def _fetch_image(url: str) -> bytes:
        if not url or not url.startswith("http"):
            return b""
        try:
            return await to_image(ctx.session, url, bytes=True)  # type: ignore[return-value]
        except Exception:
            return b""
    images = list(await asyncio.gather(*(_fetch_image(u) for u in image_urls)))
    fp = await format_bytes(
        filesize_limit=ctx.guild.filesize_limit if ctx.guild else 10485760,
        images=images,
        xbound=xbound,
        ybound=ybound,
    )
    file = discord.File(fp, "chart.png", description=" ".join(bios)[:1024])

    return file

def _parse_format(raw: str) -> tuple[int, int, int]:
    """Parse a WxH format string like '6x4'. Returns (width, height, total).
    Each side capped at 10. Falls back to 3x3 on invalid input."""
    m = re.match(r"^(\d{1,2})\s*x\s*(\d{1,2})$", raw.strip(), re.IGNORECASE)
    if not m:
        return 3, 3, 9
    w = min(int(m.group(1)), 10)
    h = min(int(m.group(2)), 10)
    return w, h, w * h


class Charts(Cog):
    @commands.hybrid_group(name="chart", fallback="albums", aliases=("c",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def chart(
        self,
        ctx: Context,
        size: str = "3x3",
        time_period: str = period,
        *,
        user: discord.User = commands.Author,
    ):
        """Displays a grid view of your top albums. Use WxH format (e.g. 5x5, 6x4). Default 3x3."""

        async with ctx.typing():
            xb, yb, n = _parse_format(size)
            await chart_cmd(ctx, user, "gettopalbums", count=n, time_period=time_period, xbound=xb, ybound=yb)

    @chart.command(name="artists", aliases=("artist", "a"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def chart_artists(
        self,
        ctx: Context,
        size: str = "3x3",
        time_period: str = period,
        *,
        user: discord.User = commands.Author,
    ):
        """Displays a grid view of your top artists"""

        async with ctx.typing():
            xb, yb, n = _parse_format(size)
            await chart_cmd(ctx, user, "gettopartists", count=n, time_period=time_period, xbound=xb, ybound=yb)

    @chart.command(name="tracks", aliases=("track", "t"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def chart_tracks(
        self,
        ctx: Context,
        size: str = "3x3",
        time_period: str = period,
        *,
        user: discord.User = commands.Author,
    ):
        """Displays a grid view of your top tracks"""

        async with ctx.typing():
            xb, yb, n = _parse_format(size)
            await chart_cmd(ctx, user, "gettoptracks", count=n, time_period=time_period, xbound=xb, ybound=yb)
