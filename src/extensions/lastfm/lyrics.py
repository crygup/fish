from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils import LayoutPager

if TYPE_CHECKING:
    from extensions.context import Context


LRCLIB_API_URL = "https://lrclib.net/api"
LRCLIB_USER_AGENT = "Fishie Discord bot (https://github.com/crygup/fish)"
DISCORD_USER_RE = re.compile(r"^(?:<@!?(?P<mention>\d{15,22})>|(?P<id>\d{15,22}))$")
SPOTIFY_TRACK_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?track/"
    r"(?P<id>[A-Za-z0-9]{10,32})(?:[/?#].*)?$",
    re.IGNORECASE,
)
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
SYNCED_TIMESTAMP_RE = re.compile(r"(?m)^\[\d{1,3}:\d{2}(?:\.\d{1,3})?]\s*")
SECTION_HEADING_RE = re.compile(r"(?m)(?=^\[[^\]\n]{1,50}]\s*$)")
MAX_LYRIC_TEXT = 3_600
MAX_LYRIC_LINES = 15


@dataclass(slots=True)
class LyricsTrack:
    title: str
    artist: str = ""
    album: str = ""
    duration: int | None = None
    # Last.fm exposes these URLs for recent tracks.  Other input sources do
    # not, so the page source derives canonical Last.fm URLs as a fallback.
    lastfm_url: str | None = None
    artist_url: str | None = None


def _discord_user_id(value: str | None) -> int | None:
    if value is None:
        return None
    match = DISCORD_USER_RE.fullmatch(value.strip())
    if not match:
        return None
    return int(match.group("mention") or match.group("id"))


def _split_long_section(section: str, limit: int = MAX_LYRIC_TEXT) -> list[str]:
    if len(section) <= limit:
        return [section]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in section.splitlines():
        line_size = len(line) + (1 if current else 0)
        if current and current_size + line_size > limit:
            chunks.append("\n".join(current))
            current = []
            current_size = 0
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_size = 0
            chunks.extend(
                line[index : index + limit] for index in range(0, len(line), limit)
            )
            continue
        current.append(line)
        current_size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def _split_lyric_lines(section: str) -> list[str]:
    """Split a section into chunks that fit both Discord limits.

    Lyrics are kept as complete lines wherever possible.  A very long line is
    still split by :func:`_split_long_section` so a malformed provider response
    cannot exceed Discord's text limit.
    """

    lines = section.splitlines() or [section]
    chunks: list[str] = []
    for index in range(0, len(lines), MAX_LYRIC_LINES):
        line_chunk = "\n".join(lines[index : index + MAX_LYRIC_LINES])
        chunks.extend(_split_long_section(line_chunk))
    return chunks


def lyric_pages(value: str) -> list[str]:
    """Split lyrics into pages containing at most fifteen lines.

    Section boundaries are retained and no more than two sections are grouped
    on a page, preserving the existing readable verse/chorus layout while
    enforcing the new line limit.
    """

    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", text) if block.strip()]
    if len(blocks) <= 1:
        heading_blocks = [
            block.strip() for block in SECTION_HEADING_RE.split(text) if block.strip()
        ]
        if len(heading_blocks) > 1:
            blocks = heading_blocks
    sections = [
        chunk
        for block in blocks or [text]
        for chunk in _split_lyric_lines(block)
        if chunk
    ]
    pages: list[str] = []
    current: list[str] = []
    current_lines = 0
    for section in sections:
        candidate = "\n\n".join((*current, section))
        section_lines = len(section.splitlines()) or 1
        if current and (
            len(current) >= 2
            or current_lines + section_lines > MAX_LYRIC_LINES
            or len(candidate) > MAX_LYRIC_TEXT
        ):
            pages.append("\n\n".join(current))
            current = []
            current_lines = 0
        current.append(section)
        current_lines += section_lines
    if current:
        pages.append("\n\n".join(current))
    return pages or ["No lyrics were returned."]


def _lastfm_artist_url(artist: str) -> str:
    """Return a canonical Last.fm artist URL for a display name."""

    value = artist.strip()
    if not value:
        return "https://www.last.fm/music"
    return f"https://www.last.fm/music/{quote(value, safe='')}"


def _lastfm_track_url(artist: str, title: str) -> str:
    """Return a canonical Last.fm track URL for a display name."""

    artist_url = _lastfm_artist_url(artist)
    if not artist.strip() or not title.strip():
        return artist_url
    return f"{artist_url}/_/{quote(title.strip(), safe='')}"


class LyricsPageSource:
    def __init__(self, track: LyricsTrack, pages: list[str]) -> None:
        self.track = track
        self.pages = pages

    def get_max_pages(self) -> int:
        return len(self.pages)

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        page = max(0, min(page_number, len(self.pages) - 1))
        safe_title = discord.utils.escape_markdown(self.track.title)
        artist = self.track.artist or "Unknown artist"
        safe_artist = discord.utils.escape_markdown(artist)
        artist_url = self.track.artist_url or _lastfm_artist_url(artist)
        track_url = self.track.lastfm_url or _lastfm_track_url(artist, self.track.title)
        safe_lyrics = discord.utils.escape_markdown(self.pages[page])
        return [
            discord.ui.TextDisplay(
                f"## Lyrics for [{safe_title}]({track_url}) by "
                f"[{safe_artist}]({artist_url})"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(safe_lyrics),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"-# Page {page + 1}/{len(self.pages)}"),
        ]


class Lyrics(Cog):
    async def _recent_lyrics_track(self, user_id: int) -> LyricsTrack:
        username = self.bot.db_cache.lastfm.get(user_id)
        if not username:
            raise commands.BadArgument(
                "This user has not connected their Last.fm account. "
                "Use `fish accounts` to connect it."
            )
        response = await self.bot.lfm_get(
            {"method": "user.getrecenttracks", "user": username, "limit": 1}
        )
        tracks = response.get("recenttracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not tracks or not isinstance(tracks[0], dict):
            raise commands.BadArgument(
                f"No recent tracks were found for **{username}**."
            )
        item = tracks[0]
        artist_data = item.get("artist")
        album_data = item.get("album")
        artist = (
            str(artist_data.get("#text") or artist_data.get("name") or "")
            if isinstance(artist_data, dict)
            else str(artist_data or "")
        )
        album = (
            str(album_data.get("#text") or album_data.get("title") or "")
            if isinstance(album_data, dict)
            else str(album_data or "")
        )
        track_url = str(item.get("url") or "").strip() or None
        artist_url = (
            str(artist_data.get("url") or "").strip() or None
            if isinstance(artist_data, dict)
            else None
        )
        title = str(item.get("name") or "").strip()
        if not title:
            raise commands.BadArgument("Last.fm did not provide a usable track.")
        return LyricsTrack(
            title=title,
            artist=artist.strip(),
            album=album.strip(),
            lastfm_url=track_url,
            artist_url=artist_url,
        )

    async def _spotify_lyrics_track(self, ctx: Context, track_id: str) -> LyricsTrack:
        token = str(getattr(self.bot, "spotify_key", "") or "")
        if not token:
            raise commands.BadArgument("Spotify is unavailable right now.")
        async with ctx.session.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status != 200:
                raise commands.BadArgument("Spotify could not find that track.")
            data = await response.json(content_type=None)
        artists = data.get("artists", []) if isinstance(data, dict) else []
        artist = ", ".join(
            str(entry.get("name"))
            for entry in artists
            if isinstance(entry, dict) and entry.get("name")
        )
        album_data = data.get("album") if isinstance(data, dict) else None
        album = (
            str(album_data.get("name") or "") if isinstance(album_data, dict) else ""
        )
        duration_ms = data.get("duration_ms") if isinstance(data, dict) else None
        return LyricsTrack(
            title=str(data.get("name") or "Unknown track"),
            artist=artist,
            album=album,
            duration=(int(duration_ms) // 1000 if duration_ms else None),
        )

    async def _youtube_lyrics_track(self, ctx: Context, url: str) -> LyricsTrack:
        async with ctx.session.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
        ) as response:
            if response.status != 200:
                raise commands.BadArgument("YouTube could not find that video.")
            data = await response.json(content_type=None)
        title = str(data.get("title") or "").strip()
        if not title:
            raise commands.BadArgument("YouTube did not provide a usable title.")
        return LyricsTrack(title=title, artist=str(data.get("author_name") or ""))

    async def _resolve_lyrics_track(
        self, ctx: Context, query: str | None
    ) -> tuple[LyricsTrack, bool]:
        raw = query.strip() if query else ""
        target_id = _discord_user_id(raw) if raw else ctx.author.id
        if target_id is not None:
            return await self._recent_lyrics_track(target_id), True

        spotify_match = SPOTIFY_TRACK_RE.fullmatch(raw)
        if spotify_match:
            return (
                await self._spotify_lyrics_track(ctx, spotify_match.group("id")),
                True,
            )

        parsed = urlsplit(raw)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            if parsed.hostname.casefold() in YOUTUBE_HOSTS:
                return await self._youtube_lyrics_track(ctx, raw), False
            raise commands.BadArgument(
                "Lyrics links must be a Spotify track or YouTube video."
            )

        if not raw:
            raise commands.BadArgument("Give me a song to find lyrics for.")
        if " - " in raw:
            artist, title = raw.split(" - ", 1)
            if artist.strip() and title.strip():
                return LyricsTrack(title=title.strip(), artist=artist.strip()), True
        return LyricsTrack(title=raw), False

    @staticmethod
    def _usable_lyrics(result: Any) -> bool:
        return isinstance(result, dict) and bool(
            result.get("plainLyrics") or result.get("syncedLyrics")
        )

    async def _lrclib_result(
        self, ctx: Context, track: LyricsTrack, *, exact: bool
    ) -> dict[str, Any]:
        headers = {"User-Agent": LRCLIB_USER_AGENT}
        result: dict[str, Any] | None = None
        if exact and track.artist:
            params: dict[str, Any] = {
                "track_name": track.title,
                "artist_name": track.artist,
            }
            if track.album:
                params["album_name"] = track.album
            if track.duration:
                params["duration"] = track.duration
            async with ctx.session.get(
                f"{LRCLIB_API_URL}/get", headers=headers, params=params
            ) as response:
                if response.status == 200:
                    payload = await response.json(content_type=None)
                    if self._usable_lyrics(payload):
                        result = payload

        if result is None:
            search_query = (
                " ".join(part for part in (track.artist, track.title) if part)
                if exact
                else track.title
            )
            async with ctx.session.get(
                f"{LRCLIB_API_URL}/search",
                headers=headers,
                params={"q": search_query},
            ) as response:
                if response.status != 200:
                    raise commands.BadArgument("LRCLIB is unavailable right now.")
                payload = await response.json(content_type=None)
            matches = payload if isinstance(payload, list) else []
            result = next(
                (entry for entry in matches if self._usable_lyrics(entry)), None
            )

        if result is None:
            raise commands.BadArgument(f"No lyrics were found for **{track.title}**.")
        return result

    @commands.hybrid_command(name="lyrics", aliases=("lyric",))
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        query=(
            "Song, Spotify track, YouTube video, or Discord user. "
            "Leave blank for your current track."
        )
    )
    async def lyrics(self, ctx: Context, *, query: str | None = None) -> None:
        """Find song lyrics, defaulting to your current Last.fm track."""

        async with ctx.typing():
            requested, exact = await self._resolve_lyrics_track(ctx, query)
            result = await self._lrclib_result(ctx, requested, exact=exact)
            synced = str(result.get("syncedLyrics") or "")
            text = str(result.get("plainLyrics") or "")
            if not text and synced:
                text = SYNCED_TIMESTAMP_RE.sub("", synced)
            track = LyricsTrack(
                title=str(result.get("trackName") or requested.title),
                artist=str(result.get("artistName") or requested.artist),
                album=str(result.get("albumName") or requested.album),
                duration=requested.duration,
                lastfm_url=requested.lastfm_url,
                artist_url=requested.artist_url,
            )
            pages = LayoutPager(
                LyricsPageSource(track, lyric_pages(text)),
                ctx=ctx,
                accent_color=ctx.embedcolor,
            )
            await pages.start(ctx)
