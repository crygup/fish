from __future__ import annotations

import asyncio
import re
from io import BytesIO
from typing import TYPE_CHECKING, Any

import discord
from cachetools import TTLCache
from discord import MediaGalleryItem, app_commands, ui
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from core import Cog
from utils import (
    LastfmTimeConverter,
    lastfm_command,
    lastfm_period,
    to_image,
    to_thread,
)
from utils.rich_text import (
    draw_inline_tokens,
    fit_inline_text,
    resolve_inline_images,
    text_font,
)

if TYPE_CHECKING:
    from extensions.context import Context


period: str = commands.param(converter=LastfmTimeConverter, default="overall")
TOPSTER_COLUMNS = 5
TOPSTER_ROWS = 5
TOPSTER_COUNT = TOPSTER_COLUMNS * TOPSTER_ROWS
TOPSTER_COVER_SIZE = 164
TOPSTER_GAP = 10
TOPSTER_MARGIN = 12
TOPSTER_WIDTH = 1280
TOPSTER_HEIGHT = (
    TOPSTER_MARGIN * 2
    + TOPSTER_ROWS * TOPSTER_COVER_SIZE
    + (TOPSTER_ROWS - 1) * TOPSTER_GAP
)
LASTFM_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f.png"
TOPSTER_COVER_CACHE: TTLCache[str, bytes] = TTLCache[str, bytes](
    maxsize=1024,
    ttl=6 * 60 * 60,
)


def _album_artist(item: dict[str, Any]) -> str:
    artist = item.get("artist")
    if isinstance(artist, dict):
        return str(artist.get("name") or artist.get("#text") or "Unknown artist")
    return str(artist or "Unknown artist")


def _album_cover_url(item: dict[str, Any]) -> str | None:
    images = item.get("image")
    if not isinstance(images, list):
        return None
    for image in reversed(images):
        if not isinstance(image, dict):
            continue
        url = str(image.get("#text") or "").strip()
        if (
            url
            and url.startswith(("https://", "http://"))
            and LASTFM_PLACEHOLDER not in url
        ):
            return re.sub(r"/u/.*/", "/u/", url)
    return None


def _topster_target_user(
    ctx: Context,
    user: discord.User | discord.Member,
) -> discord.User | discord.Member:
    if ctx.interaction is not None or user.id != ctx.author.id:
        return user
    mentions = getattr(ctx.message, "mentions", [])
    return mentions[0] if mentions else user


@to_thread
def _prepare_topster_cover(data: bytes) -> bytes | None:
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 25_000_000:
                return None
            cover = ImageOps.fit(
                image.convert("RGB"),
                (TOPSTER_COVER_SIZE, TOPSTER_COVER_SIZE),
                Image.Resampling.LANCZOS,
            )
            output = BytesIO()
            cover.save(output, "JPEG", quality=92)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _topster_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return text_font("", size, mono=True)


def _fit_topster_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    suffix = "…"
    while text and draw.textlength(f"{text}{suffix}", font=font) > width:
        text = text[:-1]
    return f"{text.rstrip()}{suffix}"


@to_thread
def _render_topster(
    entries: list[tuple[str, str, bytes]],
    inline_images: dict[str, bytes] | None = None,
) -> BytesIO:
    inline_images = inline_images or {}
    canvas = Image.new("RGBA", (TOPSTER_WIDTH, TOPSTER_HEIGHT), "black")
    grid_width = (
        TOPSTER_COLUMNS * TOPSTER_COVER_SIZE + (TOPSTER_COLUMNS - 1) * TOPSTER_GAP
    )
    text_x = TOPSTER_MARGIN + grid_width + 12
    text_width = TOPSTER_WIDTH - text_x - TOPSTER_MARGIN

    for index, (artist, album, cover_data) in enumerate(entries[:TOPSTER_COUNT]):
        row, column = divmod(index, TOPSTER_COLUMNS)
        x = TOPSTER_MARGIN + column * (TOPSTER_COVER_SIZE + TOPSTER_GAP)
        y = TOPSTER_MARGIN + row * (TOPSTER_COVER_SIZE + TOPSTER_GAP)
        with Image.open(BytesIO(cover_data)) as cover:
            canvas.paste(cover.convert("RGB"), (x, y))

        label_text = f"{artist} - {album}"
        font = _topster_font(15)
        label = fit_inline_text(
            label_text,
            font,
            15,
            inline_images,
            text_width,
        )
        draw_inline_tokens(
            canvas,
            label,
            (text_x, y + column * 24 + 2),
            font=font,
            image_size=15,
            assets=inline_images,
            fill=(240, 240, 240),
        )

    output = BytesIO()
    canvas.convert("RGB").save(output, "JPEG", quality=92, optimize=True)
    output.seek(0)
    return output


class Topster(Cog):
    async def _topster_entries(
        self,
        ctx: Context,
        lfm_user: str,
        time_period: str,
    ) -> list[tuple[str, str, bytes]]:
        response = await self.bot.lfm_get(
            {
                "method": "user.gettopalbums",
                "user": lfm_user,
                "limit": 100,
                "period": time_period,
            }
        )
        top_albums = response.get("topalbums", {})
        candidates = top_albums.get("album", []) if isinstance(top_albums, dict) else []
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not isinstance(candidates, list):
            candidates = []

        async def fetch(
            item: dict[str, Any],
        ) -> tuple[str, str, bytes] | None:
            cover_url = _album_cover_url(item)
            if cover_url is None:
                return None
            try:
                cached_cover = TOPSTER_COVER_CACHE[cover_url]
            except KeyError:
                cached_cover = None
            if cached_cover is not None:
                return (
                    _album_artist(item),
                    str(item.get("name") or "Unknown album"),
                    cached_cover,
                )
            try:
                data = await to_image(ctx.session, cover_url, bytes=True)
                if not isinstance(data, bytes):
                    return None
                cover = await _prepare_topster_cover(data)
            except Exception:
                return None
            if cover is None:
                return None
            TOPSTER_COVER_CACHE[cover_url] = cover
            return (
                _album_artist(item),
                str(item.get("name") or "Unknown album"),
                cover,
            )

        entries: list[tuple[str, str, bytes]] = []
        usable_candidates = [
            item
            for item in candidates
            if isinstance(item, dict) and _album_cover_url(item) is not None
        ]
        start = 0
        while start < len(usable_candidates) and len(entries) < TOPSTER_COUNT:
            needed = TOPSTER_COUNT - len(entries)
            batch_size = TOPSTER_COUNT if start == 0 else max(needed, 10)
            batch = usable_candidates[start : start + batch_size]
            start += len(batch)
            results = await asyncio.gather(*(fetch(item) for item in batch))
            entries.extend(result for result in results if result is not None)
        return entries[:TOPSTER_COUNT]

    @commands.hybrid_command(name="topster")
    @app_commands.describe(
        time_period="Time range to include, such as weekly or overall.",
        user="Last.fm user to look up. Defaults to yourself.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @lastfm_command()
    async def topster(
        self,
        ctx: Context,
        time_period: str = period,
        user: discord.User = commands.Author,
    ):
        """Display the user's top albums as a Topster."""
        target = _topster_target_user(ctx, user)
        try:
            lfm_user = self.bot.db_cache.lastfm[target.id]
        except KeyError:
            raise commands.BadArgument(
                "This user has not connected their last.fm account"
            )

        async with ctx.typing():
            entries = await self._topster_entries(ctx, lfm_user, time_period)
            if not entries:
                raise commands.BadArgument(
                    "No albums with usable cover art were found for that timeframe."
                )
            inline_images = await resolve_inline_images(
                ctx.session,
                [f"{artist} - {album}" for artist, album, _ in entries],
            )
            image = await _render_topster(entries, inline_images)
            filename = "topster.jpg"
            file = discord.File(image, filename)
            gallery = ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}"))
            details = ui.TextDisplay(
                f"### [{target.display_name}'s Topster]"
                f"(https://www.last.fm/user/{lfm_user}), "
                f"{lastfm_period[time_period]}.\n"
                f"-# Showing {len(entries)} albums with cover art"
            )
            container = ui.Container(
                gallery,
                details,
                accent_color=self.bot.embedcolor,
            )
            view_type = type("TopsterView", (ui.LayoutView,), {})
            view = view_type(timeout=None)
            view.add_item(container)
            await ctx.send(file=file, view=view)
