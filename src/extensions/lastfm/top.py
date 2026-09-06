from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, Union, cast

import discord
from cachetools import TTLCache
from dateutil.relativedelta import relativedelta
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import (
    LastfmTimeConverter,
    LayoutPager,
    SimplePages,
    interaction_only,
    lastfm_command,
    lastfm_period,
    plural,
)

if TYPE_CHECKING:
    from extensions.context import Context


period: str = commands.param(converter=LastfmTimeConverter, default="overall")
topMode: TypeAlias = Union[
    Literal["gettopartists"], Literal["gettopalbums"], Literal["gettoptracks"]
]
modeName = {"gettopartists": "artist", "gettopalbums": "album", "gettoptracks": "track"}
DISCORD_USER_RE = re.compile(r"^(?:<@!?(?P<mention>\d{15,22})>|(?P<id>\d{15,22}))$")
TRACK_PERIODS = {
    "overall": "overall",
    "alltime": "overall",
    "all-time": "overall",
    "all": "overall",
    "hourly": "hourly",
    "hour": "hourly",
    "1h": "hourly",
    "daily": "daily",
    "day": "daily",
    "1d": "daily",
    "weekly": "7day",
    "week": "7day",
    "7d": "7day",
    "7day": "7day",
    "monthly": "1month",
    "month": "1month",
    "1m": "1month",
    "1month": "1month",
    "quarterly": "3month",
    "3m": "3month",
    "3month": "3month",
    "half-yearly": "6month",
    "6m": "6month",
    "6month": "6month",
    "yearly": "12month",
    "year": "12month",
    "1y": "12month",
    "12m": "12month",
    "12month": "12month",
    "biyearly": "24month",
    "2y": "24month",
    "24month": "24month",
}
ARTIST_TRACK_CACHE = TTLCache[tuple[str, str], list[dict[str, Any]]](
    maxsize=128, ttl=300
)


def _track_value(value: Any, *keys: str) -> str:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if item:
                return str(item)
        return ""
    return str(value or "")


def _track_playcount(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    try:
        return int(value.get("playcount", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _sorted_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (track for track in tracks if _track_playcount(track) > 0),
        key=_track_playcount,
        reverse=True,
    )


def _track_target_id(value: str) -> int | None:
    match = DISCORD_USER_RE.fullmatch(value.strip())
    if not match:
        return None
    return int(match.group("mention") or match.group("id"))


class TrackListPageSource:
    def __init__(
        self,
        *,
        heading: str,
        subtitle: str,
        tracks: list[dict[str, Any]],
        per_page: int = 10,
    ) -> None:
        self.heading = heading
        self.subtitle = subtitle
        self.tracks = tracks
        self.per_page = per_page

    def get_max_pages(self) -> int:
        return max(1, (len(self.tracks) + self.per_page - 1) // self.per_page)

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        page_count = self.get_max_pages()
        page = max(0, min(page_number, page_count - 1))
        start = page * self.per_page
        rows: list[str] = []
        for position, track in enumerate(
            self.tracks[start : start + self.per_page], start=start + 1
        ):
            name = discord.utils.escape_markdown(str(track.get("name") or "Unknown"))
            plays = _track_playcount(track)
            rows.append(
                f"{position}. **{name}** - {plays:,} {plural(plays, False):play}"
            )
        title = discord.utils.escape_markdown(self.heading)
        subtitle = discord.utils.escape_markdown(self.subtitle)
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(f"## {title}"),
        ]
        if subtitle:
            children.append(discord.ui.TextDisplay(f"-# {subtitle}"))
        children.extend(
            [
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(rows) or "No played tracks found."),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"{len(self.tracks):,} total {plural(len(self.tracks), False):track}"
                ),
            ]
        )
        return children


class Top(Cog):
    async def _range_tracks(
        self, username: str, artist: str, period_value: str, album: str | None = None
    ) -> list[dict[str, Any]]:
        """Count actual scrobbles within an inclusive UTC date range."""
        aliases = {
            "hourly": 1 / 24,
            "daily": 1,
            "7day": 7,
            "1month": 30,
            "3month": 90,
            "6month": 180,
            "12month": 365,
            "24month": 730,
        }
        value = TRACK_PERIODS.get(
            period_value.strip().lower(), period_value.strip().lower()
        )
        end = datetime.now(timezone.utc)
        if value in aliases:
            if value.endswith("month"):
                start = end - relativedelta(months=int(value.removesuffix("month")))
            else:
                start = end - timedelta(days=aliases[value])
        else:
            dates = re.fullmatch(
                r"(\d{4}-\d{2}-\d{2})\s+(?:to\s+)?(\d{4}-\d{2}-\d{2})", value
            )
            if not dates:
                raise commands.BadArgument(
                    "Use hourly, daily, weekly, monthly, quarterly, half-yearly, yearly, biyearly, overall, or YYYY-MM-DD to YYYY-MM-DD."
                )
            try:
                start = datetime.fromisoformat(dates[1]).replace(tzinfo=timezone.utc)
                end = datetime.fromisoformat(dates[2]).replace(
                    tzinfo=timezone.utc
                ) + timedelta(days=1)
            except ValueError as exc:
                raise commands.BadArgument("That date range is invalid.") from exc
            if start >= end:
                raise commands.BadArgument(
                    "The start date must be before the end date."
                )
        counts: dict[str, dict[str, Any]] = {}
        page = 1
        while True:
            response = await self.bot.lfm_get(
                {
                    "method": "user.getrecenttracks",
                    "user": username,
                    "from": int(start.timestamp()),
                    "to": int(end.timestamp()) - 1,
                    "limit": 200,
                    "page": page,
                }
            )
            recent = response.get("recenttracks", {})
            tracks = recent.get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            for track in tracks:
                if (
                    not track.get("date")
                    or _track_value(track.get("artist"), "#text", "name").casefold()
                    != artist.casefold()
                ):
                    continue
                if (
                    album is not None
                    and _track_value(track.get("album"), "#text", "name").casefold()
                    != album.casefold()
                ):
                    continue
                name = str(track.get("name") or "")
                entry = counts.setdefault(
                    name.casefold(), {"name": name, "playcount": 0}
                )
                entry["playcount"] += 1
            if page >= int(recent.get("@attr", {}).get("totalPages", 1)) or not tracks:
                break
            page += 1
        return list(counts.values())

    def _track_query_period(
        self, query: str | None, time_period: str
    ) -> tuple[str | None, str]:
        if query and time_period == "overall":
            match = re.search(
                r"\s+(weekly|week|7d|7day|monthly|month|1m|1month|3m|3month|6m|6month|yearly|year|12m|12month|overall|\d{4}-\d{2}-\d{2}\s+(?:to\s+)?\d{4}-\d{2}-\d{2})$",
                query,
                re.I,
            )
            if match:
                return query[: match.start()].strip(), match[1]
        return query, time_period

    def _track_arguments(
        self, query: str | None, time_period: str, user_id: int
    ) -> tuple[str, str, int]:
        remaining = []
        periods = []
        users = []
        for token in (query or "").split():
            target = _track_target_id(token)
            if target is not None:
                users.append(target)
            elif token.lower() in TRACK_PERIODS:
                periods.append(TRACK_PERIODS[token.lower()])
            else:
                remaining.append(token)
        if len(set(users)) > 1 or len(set(periods)) > 1:
            raise commands.BadArgument("Specify one user and one timeframe.")
        value, selected = self._track_query_period(
            " ".join(remaining),
            (
                periods[0]
                if periods
                else TRACK_PERIODS.get(time_period.lower(), time_period)
            ),
        )
        return value or "", selected, users[0] if users else user_id

    async def _artist_user_tracks(
        self, username: str, artist: str
    ) -> list[dict[str, Any]]:
        # Fetch personal counts directly, rather than enriching the artist's
        # global top 100 one request at a time (which misses less popular songs).
        key = (username.casefold(), artist.casefold())
        if key in ARTIST_TRACK_CACHE:
            return [dict(track) for track in ARTIST_TRACK_CACHE[key]]
        result: list[dict[str, Any]] = []

        async def fetch_page(page: int) -> dict[str, Any]:
            response = await self.bot.lfm_get(
                {
                    "method": "user.gettoptracks",
                    "user": username,
                    "period": "overall",
                    "limit": 1000,
                    "page": page,
                }
            )
            if response.get("error"):
                raise commands.BadArgument(
                    "Last.fm could not load this track chart. Please try again shortly."
                )
            return response.get("toptracks", {})

        def collect(data: dict[str, Any]) -> None:
            tracks = data.get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            result.extend(
                track
                for track in tracks
                if _track_value(track.get("artist"), "name", "#text").casefold()
                == artist.casefold()
            )

        first = await fetch_page(1)
        collect(first)
        total_pages = int(first.get("@attr", {}).get("totalPages", 1))
        # Pages are sorted by playcount. Once 100 matches have been found,
        # later pages cannot improve that artist's top 100.
        for start in range(2, total_pages + 1, 4):
            if len(result) >= 100:
                break
            pages = await asyncio.gather(
                *(
                    fetch_page(page)
                    for page in range(start, min(start + 4, total_pages + 1))
                )
            )
            for data in pages:
                collect(data)
        ranked = _sorted_tracks(result)[:100]
        ARTIST_TRACK_CACHE[key] = ranked
        return [dict(track) for track in ranked]

    async def _start_track_pages(
        self,
        ctx: Context,
        *,
        heading: str,
        subtitle: str,
        tracks: list[dict[str, Any]],
    ) -> None:
        tracks = _sorted_tracks(tracks)[:100]
        pages = LayoutPager(
            TrackListPageSource(
                heading=heading,
                subtitle=subtitle,
                tracks=tracks,
            ),
            ctx=ctx,
            accent_color=ctx.embedcolor,
        )
        await pages.start(ctx)

    async def _user_track_playcounts(
        self,
        tracks: list[dict[str, Any]],
        artist: str,
        username: str,
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(5)

        async def enrich(track: dict[str, Any]) -> dict[str, Any]:
            name = str(track.get("name") or "").strip()
            if not name:
                return track
            async with semaphore:
                try:
                    response = await self.bot.lfm_get(
                        {
                            "method": "track.getInfo",
                            "artist": artist,
                            "track": name,
                            "username": username,
                        }
                    )
                except Exception:
                    enriched = dict(track)
                    enriched["playcount"] = 0
                    return enriched
            info = response.get("track", {}) if isinstance(response, dict) else {}
            if not isinstance(info, dict):
                info = {}
            enriched = dict(track)
            enriched["playcount"] = info.get("userplaycount", 0)
            enriched["url"] = info.get("url") or enriched.get("url")
            return enriched

        return list(await asyncio.gather(*(enrich(track) for track in tracks)))

    async def _show_album_tracks(
        self,
        ctx: Context,
        query: str | None,
        time_period: str = "overall",
        user: discord.User | None = None,
    ) -> None:
        value, time_period, target_id = self._track_arguments(
            query, time_period, user.id if user else ctx.author.id
        )
        username = self.bot.db_cache.lastfm.get(target_id or ctx.author.id)
        if not username:
            raise commands.BadArgument(
                "Connect your Last.fm account with `fish accounts` to view your "
                "album track playcounts."
            )
        if not value:
            artist, album = await cast(Any, self)._lastfm_current_album(username)
            value = f"{artist} - {album}"
        item, album_name = await cast(Any, self)._entity_response(
            "album", value, username=username
        )
        artist = _track_value(item.get("artist"), "name", "#text")
        track_data = item.get("tracks", {})
        tracks = track_data.get("track", []) if isinstance(track_data, dict) else []
        if isinstance(tracks, dict):
            tracks = [tracks]
        tracks = [track for track in tracks if isinstance(track, dict)]
        if not tracks:
            raise commands.BadArgument(f"No tracks were found for **{album_name}**.")
        tracks = (
            await self._user_track_playcounts(tracks, artist, username)
            if time_period == "overall"
            else await self._range_tracks(
                username, artist, time_period, str(item.get("name") or album_name)
            )
        )
        await self._start_track_pages(
            ctx,
            heading=f"{username}'s top tracks for {item.get('name') or album_name} for {artist}",
            subtitle="",
            tracks=tracks,
        )

    async def _show_artist_tracks(
        self,
        ctx: Context,
        query: str | None,
        time_period: str = "overall",
        user: discord.User | None = None,
    ) -> None:
        value, time_period, target_id = self._track_arguments(
            query, time_period, user.id if user else ctx.author.id
        )
        username = self.bot.db_cache.lastfm.get(target_id or ctx.author.id)
        if not username:
            raise commands.BadArgument(
                "Connect your Last.fm account with `fish accounts` to view your "
                "artist track playcounts."
            )
        if not value:
            value, _ = await cast(Any, self)._lastfm_current_track(username)
        artist_info, artist_name = await cast(Any, self)._entity_response(
            "artist", value, username=username
        )
        artist_name = str(artist_info.get("name") or artist_name)
        tracks = (
            await self._artist_user_tracks(username, artist_name)
            if time_period == "overall"
            else await self._range_tracks(username, artist_name, time_period)
        )
        await self._start_track_pages(
            ctx,
            heading=f"{username}'s top tracks for {artist_name}",
            subtitle="",
            tracks=tracks,
        )

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
                "This user has not connected their Last.fm account. "
                "Use `fish accounts` to connect it."
            )

        data = {
            "method": f"user.{mode}",
            "user": lfm_user,
            "limit": 100,
            "period": time_period,
        }

        response = (await self.bot.lfm_get(data))[f"top{modeName[mode]}s"]
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
    @app_commands.describe(
        time_period="Time range to include, such as weekly or overall.",
        user="Last.fm user to look up. Defaults to yourself.",
    )
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
    @app_commands.describe(
        time_period="Time range to include, such as weekly or overall.",
        user="Last.fm user to look up. Defaults to yourself.",
    )
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
    @app_commands.describe(
        time_period="Time range to include, such as weekly or overall.",
        user="Last.fm user to look up. Defaults to yourself.",
    )
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

    @commands.hybrid_command(name="albumtracks", aliases=("albumtrack", "abt"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query=(
            "Album name, Artist - Album, or Discord user. "
            "Defaults to your current album."
        )
    )
    async def album_tracks(
        self,
        ctx: Context,
        time_period: str = "overall",
        *,
        query: str | None = None,
        user: discord.User | None = None,
    ) -> None:
        """List an album's tracks sorted by playcount."""

        async with ctx.typing():
            if ctx.interaction is None:
                query = (
                    " ".join(
                        part
                        for part in (
                            time_period if time_period != "overall" else "",
                            query,
                        )
                        if part
                    )
                    or None
                )
                time_period = "overall"
            await self._show_album_tracks(ctx, query, time_period, user)

    @commands.hybrid_command(name="artisttracks", aliases=("artisttrack", "at"))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query="Artist name or Discord user. Defaults to your current artist."
    )
    async def artist_tracks(
        self,
        ctx: Context,
        time_period: str = "overall",
        *,
        query: str | None = None,
        user: discord.User | None = None,
    ) -> None:
        """List an artist's tracks sorted by playcount."""

        async with ctx.typing():
            if ctx.interaction is None:
                query = (
                    " ".join(
                        part
                        for part in (
                            time_period if time_period != "overall" else "",
                            query,
                        )
                        if part
                    )
                    or None
                )
                time_period = "overall"
            await self._show_artist_tracks(ctx, query, time_period, user)
