from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from extensions.events.youtube import normalize_youtube_events
from utils import (
    AuthorView,
    GoogleImageData,
    LayoutPager,
    Pager,
    response_checker,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context, GuildContext

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


class GoogleImageLayoutSource:
    """Components V2 page source for Google image result URLs."""

    def __init__(self, entries: list[GoogleImageData]) -> None:
        self.entries = entries

    def get_max_pages(self) -> int:
        return len(self.entries)

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        entry = self.entries[page_number]
        title = _image_display_text(entry.snippet)

        return [
            discord.ui.TextDisplay(f"## {title}"),
            discord.ui.MediaGallery(discord.MediaGalleryItem(entry.image_url)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"-# Page {page_number + 1}/{self.get_max_pages()} · "
                f"Google Image search: {_image_display_text(entry.query, 300)}"
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
        aliases=("yt", "ytnotify", "youtube-notifications"),
        fallback="video",
        extras={"google-command": True},
    )
    @app_commands.describe(query="Video to search for")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def youtube(self, ctx: Context, *, query: str):
        """Search YouTube or manage this server's channel notifications."""
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

    @youtube.command(
        name="list",
        description="List the YouTube channels followed by this server.",
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def youtube_list(self, ctx: GuildContext):
        """List YouTube channels followed by this server."""
        rows = await self.bot.pool.fetch(
            "SELECT channel_name, channel_handle, announce_channel_id, event_types "
            "FROM youtube_follows WHERE guild_id = $1 ORDER BY channel_name",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.send("No YouTube channels are being followed.")
        lines = [
            f"**{row['channel_name']}** → <#{row['announce_channel_id']}>"
            f" ({', '.join(row['event_types'])})"
            for row in rows
        ]
        await ctx.send("YouTube notifications:\n" + "\n".join(lines))

    @youtube.command(
        name="follow",
        description="Follow a YouTube channel in this server.",
    )
    @app_commands.describe(
        channel="YouTube channel handle, channel ID, or URL.",
        announcement_channel="Text channel for notifications. Defaults to this channel.",
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def youtube_follow(
        self,
        ctx: GuildContext,
        channel: str,
        announcement_channel: Optional[discord.TextChannel] = None,
    ):
        """Follow a YouTube channel for videos, live streams, Shorts, and posts."""
        events: Any = self.bot.get_cog("Events")
        if events is None or not hasattr(events, "resolve_youtube_channel"):
            raise commands.BadArgument(
                "YouTube notifications are unavailable right now."
            )
        youtube_channel = await events.resolve_youtube_channel(channel)
        if youtube_channel is None:
            raise commands.BadArgument(
                "Could not find that YouTube channel. Use an @handle or channel URL."
            )
        target = announcement_channel or ctx.channel
        if not hasattr(target, "send"):
            raise commands.BadArgument(
                "Choose a text channel for YouTube notifications."
            )
        channel_id = str(youtube_channel["id"])
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"fishie:youtube:{ctx.guild.id}",
                )
                existing = await connection.fetchval(
                    "SELECT 1 FROM youtube_follows "
                    "WHERE guild_id = $1 AND youtube_channel_id = $2",
                    ctx.guild.id,
                    channel_id,
                )
                if not existing:
                    count = await connection.fetchval(
                        "SELECT COUNT(*) FROM youtube_follows WHERE guild_id = $1",
                        ctx.guild.id,
                    )
                    if count >= 3:
                        raise commands.BadArgument(
                            "You can follow up to 3 YouTube channels per server."
                        )
                await connection.execute(
                    """
                    INSERT INTO youtube_follows
                        (guild_id, youtube_channel_id, channel_name, channel_handle,
                         announce_channel_id)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (guild_id, youtube_channel_id) DO UPDATE SET
                        channel_name = EXCLUDED.channel_name,
                        channel_handle = EXCLUDED.channel_handle,
                        announce_channel_id = EXCLUDED.announce_channel_id,
                        updated_at = now()
                    """,
                    ctx.guild.id,
                    channel_id,
                    youtube_channel["name"],
                    youtube_channel.get("handle"),
                    target.id,
                )
        await events.ensure_youtube_subscription(channel_id)
        await ctx.send(
            f"Now following **{youtube_channel['name']}** in {target.mention}. "
            "Videos, live streams, Shorts, and community posts are enabled."
        )

    @youtube.command(
        name="events",
        description="Choose which YouTube events this server receives.",
    )
    @app_commands.describe(
        channel="Followed YouTube channel name, handle, ID, or URL.",
        events="Event types: video, live, short, and community.",
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def youtube_events(self, ctx: GuildContext, channel: str, *, events: str):
        """Choose video, live, short, and community notification types."""
        selected = normalize_youtube_events(events)
        if not selected:
            raise commands.BadArgument(
                "Choose at least one of: video, live, short, community."
            )
        result = await self.bot.pool.execute(
            "UPDATE youtube_follows SET event_types = $3, updated_at = now() "
            "WHERE guild_id = $1 AND "
            "(lower(channel_name) = lower($2) OR lower(channel_handle) = lower($2) "
            "OR youtube_channel_id = $2)",
            ctx.guild.id,
            channel.strip(),
            list(selected),
        )
        if result == "UPDATE 0":
            raise commands.BadArgument("This server is not following that channel.")
        await ctx.send(f"YouTube notifications set to: **{', '.join(selected)}**.")

    @youtube.command(
        name="message",
        aliases=("customize", "text"),
        description="Set optional text above a YouTube notification.",
    )
    @app_commands.describe(
        channel="Followed YouTube channel name, handle, ID, or URL.",
        message="Text to post above the notification. Use clear to remove it.",
    )
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def youtube_message(
        self, ctx: GuildContext, channel: str, *, message: str = ""
    ):
        """Set optional text above a YouTube notification."""
        value = message.strip()
        if value.lower() in {"clear", "none", "off"}:
            value = ""
        if len(value) > 2000:
            raise commands.BadArgument("The message cannot exceed 2000 characters.")
        result = await self.bot.pool.execute(
            "UPDATE youtube_follows SET message_template = $3, updated_at = now() "
            "WHERE guild_id = $1 AND "
            "(lower(channel_name) = lower($2) OR lower(channel_handle) = lower($2) "
            "OR youtube_channel_id = $2)",
            ctx.guild.id,
            channel.strip(),
            value or None,
        )
        if result == "UPDATE 0":
            raise commands.BadArgument("This server is not following that channel.")
        await ctx.send(
            "The YouTube announcement text was saved."
            if value
            else "The YouTube announcement text was cleared."
        )

    @youtube.command(
        name="unfollow",
        aliases=("remove", "delete"),
        description="Stop following a YouTube channel in this server.",
    )
    @app_commands.describe(channel="Followed YouTube channel name, handle, ID, or URL.")
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    @commands.has_guild_permissions(manage_guild=True)
    @commands.guild_only()
    async def youtube_unfollow(self, ctx: GuildContext, *, channel: str):
        """Stop following a YouTube channel."""
        row = await self.bot.pool.fetchrow(
            "DELETE FROM youtube_follows WHERE guild_id = $1 AND "
            "(lower(channel_name) = lower($2) OR lower(channel_handle) = lower($2) "
            "OR youtube_channel_id = $2) RETURNING youtube_channel_id, channel_name",
            ctx.guild.id,
            channel.strip(),
        )
        if row is None:
            raise commands.BadArgument("This server is not following that channel.")
        events: Any = self.bot.get_cog("Events")
        if events is not None and hasattr(events, "remove_youtube_subscription"):
            await events.remove_youtube_subscription(str(row["youtube_channel_id"]))
        await ctx.send(f"Stopped following **{row['channel_name']}**.")


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
