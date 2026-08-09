from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import TYPE_CHECKING, Any, Dict, List, Literal, TypeAlias, Union

import discord
from cachetools import TTLCache
from discord import MediaGalleryItem, app_commands, ui
from discord.ext import commands

from core import Cog
from utils import (
    LastfmTimeConverter,
    format_bytes,
    lastfm_command,
    lastfm_period,
    response_checker,
    to_image,
)

if TYPE_CHECKING:
    from extensions.context import Context


period: str = commands.param(converter=LastfmTimeConverter, default="overall")
topMode: TypeAlias = Union[
    Literal["gettopartists"], Literal["gettopalbums"], Literal["gettoptracks"]
]
modeName = {"gettopartists": "artist", "gettopalbums": "album", "gettoptracks": "track"}
SPOTIFY_COVER_CACHE: TTLCache[tuple[str, str, str], str] = TTLCache[
    tuple[str, str, str], str
](maxsize=512, ttl=3600)
SPOTIFY_METADATA_CACHE: TTLCache[tuple[str, str, str], dict[str, Any]] = TTLCache[
    tuple[str, str, str], dict[str, Any]
](maxsize=512, ttl=3600)
LASTFM_BLANK_COVER = "2a96cbd8b46e442fc41c2b86b821562f.png"


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

    data = {"method": "user.getrecenttracks", "user": lfm_user}
    image_task = asyncio.create_task(
        make_image(
            ctx,
            lfm_user,
            mode,
            count,
            time_period=time_period,
            xbound=xbound,
            ybound=ybound,
        )
    )
    total_task = asyncio.create_task(ctx.bot.lfm_get(data))
    file, response = await asyncio.gather(image_task, total_task)
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


async def search_spotify_data(
    ctx: Context,
    mode: str,
    title: str,
    artist: str | None = None,
) -> dict[str, Any] | None:
    normalized_title = _normalize_match(title)
    normalized_artist = _normalize_match(artist or "")
    cache_key = (mode, normalized_title, normalized_artist)
    try:
        return SPOTIFY_METADATA_CACHE[cache_key]
    except KeyError:
        pass

    url = "https://api.spotify.com/v1/search"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ctx.bot.spotify_key}",
    }

    query = title
    if mode == "track" and artist:
        query = f'track:"{title}" artist:"{artist}"'
    elif mode == "album" and artist:
        query = f'album:"{title}" artist:"{artist}"'
    api_data = {"q": query, "type": mode, "limit": "10", "market": "US"}

    async with ctx.session.get(url, headers=headers, params=api_data) as resp:
        response_checker(resp)
        data = await resp.json()

        items = data.get(f"{mode}s", {}).get("items", [])
        if not items:
            return None

        for item in items:
            if not isinstance(item, dict):
                continue
            if _normalize_match(str(item.get("name") or "")) != normalized_title:
                continue
            spotify_artists = item.get("artists")
            if artist and (
                not isinstance(spotify_artists, list)
                or normalized_artist
                not in {
                    _normalize_match(str(candidate.get("name") or ""))
                    for candidate in spotify_artists
                    if isinstance(candidate, dict)
                }
            ):
                continue
            SPOTIFY_METADATA_CACHE[cache_key] = item
            image_source = item.get("album") if mode == "track" else item
            images = (
                image_source.get("images") if isinstance(image_source, dict) else None
            )
            if isinstance(images, list) and images:
                cover_url = (
                    images[0].get("url") if isinstance(images[0], dict) else None
                )
                if cover_url:
                    SPOTIFY_COVER_CACHE[cache_key] = str(cover_url)
            return item
        return None


async def search_spotify(
    ctx: Context,
    mode: str,
    title: str,
    artist: str | None = None,
) -> str | None:
    normalized_title = _normalize_match(title)
    normalized_artist = _normalize_match(artist or "")
    cache_key = (mode, normalized_title, normalized_artist)
    try:
        return SPOTIFY_COVER_CACHE[cache_key]
    except KeyError:
        pass

    item = await search_spotify_data(ctx, mode, title, artist)
    if not item:
        return None
    image_source = item.get("album") if mode == "track" else item
    images = image_source.get("images") if isinstance(image_source, dict) else None
    if not isinstance(images, list) or not images:
        return None
    cover_url = images[0].get("url") if isinstance(images[0], dict) else None
    if not cover_url:
        return None
    SPOTIFY_COVER_CACHE[cache_key] = str(cover_url)
    return str(cover_url)


def _normalize_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _lastfm_artist_name(item: dict[str, Any]) -> str | None:
    artist = item.get("artist")
    if isinstance(artist, dict):
        value = artist.get("name") or artist.get("#text")
        return str(value) if value else None
    return str(artist) if artist else None


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
        "limit": min(100, max(count * 3, count + 10, 1)),
        "period": time_period,
    }
    response = (await ctx.bot.lfm_get(data))[f"top{modeName[mode]}s"]
    items: List[Dict[Any, Any]] = response[modeName[mode]]

    # Phase 1: collect items and resolve Spotify for missing covers in parallel
    image_urls: list[str | None] = []
    spotify_tasks: list[tuple[int, str, str, str | None]] = []

    for i, item in enumerate(items):
        url = re.sub(r"/u/.*/", "/u/", item["image"][-1]["#text"])
        if LASTFM_BLANK_COVER in url:
            spotify_tasks.append(
                (
                    i,
                    modeName[mode],
                    str(item["name"]),
                    _lastfm_artist_name(item),
                )
            )
            url = ""
        image_urls.append(url)

    if spotify_tasks:

        async def _resolve_spotify(
            idx: int,
            spotify_mode: str,
            title: str,
            artist: str | None,
        ) -> tuple[int, str | None]:
            try:
                return idx, await search_spotify(
                    ctx,
                    spotify_mode,
                    title,
                    artist,
                )
            except Exception:
                return idx, None

        results = await asyncio.gather(*(_resolve_spotify(*t) for t in spotify_tasks))
        for idx, url in results:
            image_urls[idx] = url

    selected = [
        (item, url) for item, url in zip(items, image_urls, strict=False) if url
    ][:count]
    if not selected:
        raise commands.BadArgument(
            "No chart entries with matching cover art were found."
        )

    bios: list[str] = []
    final_urls: list[str] = []
    for index, (item, url) in enumerate(selected, start=1):
        artist = _lastfm_artist_name(item)
        name = f"{item['name']} by {artist}" if artist else str(item["name"])
        bios.append(f"#{index} {name}")
        final_urls.append(str(url))

    # Phase 2: download all images in parallel
    async def _fetch_image(url: str) -> bytes:
        if not url or not url.startswith("http"):
            return b""
        try:
            return await to_image(ctx.session, url, bytes=True)  # type: ignore[return-value]
        except Exception:
            return b""

    images = list(await asyncio.gather(*(_fetch_image(url) for url in final_urls)))
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
    @app_commands.describe(
        size="Chart grid size such as 3x3 or 5x5.",
        time_period="Time range to include, such as weekly or overall.",
        user="Last.fm user to look up. Defaults to yourself.",
    )
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
            await chart_cmd(
                ctx,
                user,
                "gettopalbums",
                count=n,
                time_period=time_period,
                xbound=xb,
                ybound=yb,
            )

    @chart.command(name="artists", aliases=("artist", "a"))
    @app_commands.describe(
        size="Chart grid size such as 3x3 or 5x5.",
        time_period="Time range to include, such as weekly or overall.",
        user="Last.fm user to look up. Defaults to yourself.",
    )
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
            await chart_cmd(
                ctx,
                user,
                "gettopartists",
                count=n,
                time_period=time_period,
                xbound=xb,
                ybound=yb,
            )

    @chart.command(name="tracks", aliases=("track", "t"))
    @app_commands.describe(
        size="Chart grid size such as 3x3 or 5x5.",
        time_period="Time range to include, such as weekly or overall.",
        user="Last.fm user to look up. Defaults to yourself.",
    )
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
            await chart_cmd(
                ctx,
                user,
                "gettoptracks",
                count=n,
                time_period=time_period,
                xbound=xb,
                ybound=yb,
            )
