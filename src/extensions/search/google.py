from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any, Dict, List
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import (
    AuthorView,
    GoogleImageData,
    LayoutPager,
    Pager,
    response_checker,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context

id_converter = {
    "video": "videoId",
    "channel": "channelId",
    "playlist": "playlistId",
}

link_converter = {
    "video": "watch?v=",
    "channel": "channel/",
    "playlist": "playlist?list=",
}


def _discord_component_text(value: object, limit: int = 100) -> str:
    """Keep component labels within Discord's UTF-16 length limit."""
    text = " ".join(str(value or "").split()) or "Untitled"
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text

    suffix = "…"
    budget = limit - (len(suffix.encode("utf-16-le")) // 2)
    result = ""
    for character in text:
        candidate = result + character
        if len(candidate.encode("utf-16-le")) // 2 > budget:
            break
        result = candidate
    return result.rstrip() + suffix


def _image_display_text(value: object, limit: int = 1_500) -> str:
    """Keep Google result text safe and within Components V2 limits."""
    text = discord.utils.escape_mentions(
        discord.utils.escape_markdown(" ".join(str(value or "").split()))
    ).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text or "Image result"


IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".ico",
        ".jfif",
        ".jpeg",
        ".jpg",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    }
)


def _looks_like_image_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    path = urlsplit(value.strip()).path.casefold()
    return any(path.endswith(extension) for extension in IMAGE_EXTENSIONS)


def _image_source_link(value: object) -> str:
    """Return a compact Markdown link to an image result's source page."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    hostname = parsed.hostname.casefold().rstrip(".").removeprefix("www.")
    if not hostname:
        return ""
    safe_hostname = discord.utils.escape_markdown(hostname)
    safe_url = url.replace("(", "%28").replace(")", "%29")
    return f"[{safe_hostname}]({safe_url})"


class GoogleImageLayoutSource:
    """Components V2 page source for Google image result URLs."""

    def __init__(self, entries: list[GoogleImageData]) -> None:
        self.entries = entries

    def get_max_pages(self) -> int:
        return len(self.entries)

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        entry = self.entries[page_number]
        title = _image_display_text(entry.snippet)
        source_link = _image_source_link(entry.url)
        source_suffix = f" · {source_link}" if source_link else ""

        return [
            discord.ui.TextDisplay(f"### {title}"),
            discord.ui.MediaGallery(discord.MediaGalleryItem(entry.image_url)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"-# Page {page_number + 1}/{self.get_max_pages()} · "
                f"Google Image search: {_image_display_text(entry.query, 300)}"
                f"{source_suffix}"
            ),
        ]


class Google(Cog):
    def __init__(self, bot: Fishie) -> None:
        self.bot = bot

    @commands.hybrid_command(name="google")
    @app_commands.describe(query="Search terms to look up on the web.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def google(self, ctx: Context, *, query: str):
        """Search the web with Google."""

        url = "https://customsearch.googleapis.com/customsearch/v1"
        params = {
            "cx": self.bot.config["keys"]["google_id"],
            "q": query,
            "key": random.choice(self.bot.config["keys"]["google"]),
            "safe": (
                "off"
                if isinstance(
                    ctx.channel,
                    (
                        discord.DMChannel,
                        discord.PartialMessageable,
                        discord.GroupChannel,
                    ),
                )
                else ["active", "off"][ctx.channel.is_nsfw()]
            ),
        }
        await ctx.typing()

        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()

            embed = discord.Embed(color=discord.Colour.pink())
            embed.set_footer(
                text=f"About {data['searchInformation']['formattedTotalResults']} results ({data['searchInformation']['formattedSearchTime']} seconds)"
            )

            embed.title = f"Google Search - {query}"[:256]

            text = ""
            items = data["items"]

            added = 0
            for item in items:
                if added == 5:
                    break
                try:
                    text += f"[{item['title']}]({item['link']})\n{item['snippet']}\n\n"
                    added += 1
                except KeyError:
                    continue
            embed.description = text

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="image",
        aliases=("img", "i"),
        extras={"google-command": True},
    )
    @app_commands.describe(query="Search terms to look up in Google Images.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def google_image(self, ctx: Context, *, query: str):
        """Search Google Images."""
        url = "https://customsearch.googleapis.com/customsearch/v1"
        params = {
            "cx": self.bot.config["keys"]["google_id"],
            "q": query,
            "key": random.choice(self.bot.config["keys"]["google"]),
            "searchType": "image",
            "num": 10,
            "safe": (
                "off"
                if isinstance(
                    ctx.channel,
                    (
                        discord.DMChannel,
                        discord.PartialMessageable,
                        discord.GroupChannel,
                    ),
                )
                else ["active", "off"][ctx.channel.is_nsfw()]
            ),
        }

        await ctx.typing()

        async def fetch_items(start: int) -> list[object]:
            request_params = dict(params)
            if start > 1:
                request_params["start"] = start
            async with self.bot.session.get(url, params=request_params) as response:
                response_checker(response)
                data = await response.json()
            return data.get("items") or []

        items = await fetch_items(1)
        if not items:
            raise commands.BadArgument("No search results found for this query.")

        entries: list[GoogleImageData] = []
        next_start = 1
        for _ in range(10):
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                image_url = str(item.get("link") or "").strip()
                if not _looks_like_image_url(image_url):
                    continue
                image_info = item.get("image")
                context_url = (
                    image_info.get("contextLink")
                    if isinstance(image_info, dict)
                    else None
                )
                entries.append(
                    GoogleImageData(
                        image_url=image_url,
                        url=str(context_url or image_url),
                        snippet=str(item.get("title") or item.get("snippet") or query),
                        query=query,
                        author=ctx.author,
                    )
                )
                if len(entries) >= 10:
                    break
            if len(entries) >= 10:
                break
            next_start += 10
            items = await fetch_items(next_start)

        if not entries:
            raise commands.BadArgument("No image results found for this query.")

        pager = LayoutPager(GoogleImageLayoutSource(entries), ctx=ctx)
        await pager.start(ctx)

    async def search_method(
        self,
        ctx: Context,
        query: str,
        type: str,
    ):
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "q": query,
            "key": random.choice(self.bot.config["keys"]["google"]),
            "part": "snippet",
            "type": type,
            "maxResults": 25,
        }

        await ctx.typing()
        async with self.bot.session.get(url, params=params) as r:
            response_checker(r)
            data = await r.json()
            try:
                url = f"https://www.youtube.com/{link_converter[type]}{data['items'][0]['id'][id_converter[type]]}"
            except (IndexError, KeyError):
                raise commands.BadArgument("Couldn't find any results.")

        videos = data["items"]
        view = YoutubeView(ctx, videos, type)
        await ctx.send(url, view=view)

    @commands.hybrid_group(
        name="youtube",
        aliases=("yt",),
        fallback="video",
        extras={"google-command": True},
    )
    @app_commands.describe(query="Video to search for")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def youtube(self, ctx: Context, *, query: str):
        """Search YouTube videos."""
        await self.search_method(ctx, query, "video")

    @youtube.command(
        name="channel",
        aliases=("ch",),
        description="Search for a YouTube channel.",
    )
    @app_commands.describe(query="Channel to search for")
    async def youtube_channel(self, ctx: Context, *, query: str):
        await self.search_method(ctx, query, "channel")

    @youtube.command(
        name="playlist",
        aliases=("pl",),
        description="Search for a YouTube playlist.",
    )
    @app_commands.describe(query="Playlist to search for")
    async def youtube_playlist(self, ctx: Context, *, query: str):
        await self.search_method(ctx, query, "playlist")


class YoutubeDropdown(discord.ui.Select):
    def __init__(self, videos: List[Dict[Any, Any]], type: str):
        self._type = type
        self.videos = videos

        start = 0
        options = []
        for vid in videos:
            options.append(
                discord.SelectOption(
                    label=_discord_component_text(vid["snippet"].get("title")),
                    value=str(start),
                    emoji="<:yt:1097399470842466334>",
                )
            )
            start += 1

        super().__init__(
            placeholder=_discord_component_text(videos[0]["snippet"].get("title")),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> Any:
        if interaction.message is None:
            await interaction.response.defer()
            return

        index = self.values[0]
        link = link_converter[self._type]
        data = self.videos[int(index)]
        url = f"https://www.youtube.com/{link}{data['id'][id_converter[self._type]]}"
        self.placeholder = data["snippet"]["title"]
        await interaction.message.edit(content=url, view=self.view)
        await interaction.response.defer()


class YoutubeView(AuthorView):
    def __init__(self, ctx: Context, videos: List[Dict[Any, Any]], type):
        super().__init__(ctx=ctx)
        self.add_item(YoutubeDropdown(videos, type))
