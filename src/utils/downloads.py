from __future__ import annotations

import asyncio
import glob
import html as html_lib
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from io import BufferedReader, BytesIO
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord import MediaGalleryItem, ui
from discord.ext import commands

from .errors import DownloadError
from .functions import to_thread
from .network import (
    read_bounded_response,
    validate_connected_peer,
    validate_public_url,
)
from .paths import DOWNLOADS_ROOT, FILES_ROOT
from .regexes import (
    FACEBOOK_RE,
    INSTAGRAM_RE,
    KLIPY_RE,
    LIVE_STREAM_RE,
    PIXIV_RE,
    REDDIT_RE,
    SOUNDCLOUD_RE,
    THREADS_RE,
    TIKTOK_RE,
    TUMBLR_RE,
    TWITTER_RE,
    YOUTUBE_RE,
    YT_CLIP_RE,
    YT_SHORT_RE,
)
from .temp_media import TemporaryMediaError, upload_temporary_media
from .vars import base_header

if TYPE_CHECKING:
    from core import Context

DOWNLOAD_TIMEOUT: float = 10 * 60.0  # ten minutes
DOWNLOAD_TIMEOUT_MINUTES = int(DOWNLOAD_TIMEOUT // 60)
DOWNLOAD_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "instagram.com",
        "www.instagram.com",
        "kkinstagram.com",
        "www.kkinstagram.com",
        "tiktok.com",
        "www.tiktok.com",
        "kktiktok.com",
        "www.kktiktok.com",
        "m.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "vk.tiktok.com",
        "soundcloud.com",
        "on.soundcloud.com",
        "twitter.com",
        "www.twitter.com",
        "x.com",
        "www.x.com",
        "fxtwitter.com",
        "vxtwitter.com",
        "fixupx.com",
        "girlcockx.com",
        "clips.twitch.tv",
        "twitch.tv",
        "www.twitch.tv",
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "new.reddit.com",
        "np.reddit.com",
        "sh.reddit.com",
        "nm.reddit.com",
        "redditmedia.com",
        "www.redditmedia.com",
        "redd.it",
        "i.redd.it",
        "v.redd.it",
        "preview.redd.it",
        "external-preview.redd.it",
        "threads.net",
        "www.threads.net",
        "threads.com",
        "www.threads.com",
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "web.facebook.com",
        "fb.watch",
        "bsky.app",
        "www.bsky.app",
        "newgrounds.com",
        "www.newgrounds.com",
        "pixiv.net",
        "www.pixiv.net",
        "tumblr.com",
        "www.tumblr.com",
        "pin.it",
        "pinterest.com",
        "www.pinterest.com",
        "static.klipy.com",
        "media.tenor.com",
        "media1.tenor.com",
        "c.tenor.com",
    }
)
_DOWNLOAD_SITE_HOSTS: dict[str, frozenset[str]] = {
    "instagram": frozenset(
        {
            "instagram.com",
            "www.instagram.com",
            "kkinstagram.com",
            "www.kkinstagram.com",
        }
    ),
    "tiktok": frozenset(
        {
            "tiktok.com",
            "www.tiktok.com",
            "kktiktok.com",
            "www.kktiktok.com",
            "m.tiktok.com",
            "vm.tiktok.com",
            "vt.tiktok.com",
            "vk.tiktok.com",
        }
    ),
    "twitter": frozenset(
        {
            "twitter.com",
            "www.twitter.com",
            "x.com",
            "www.x.com",
            "fxtwitter.com",
            "www.fxtwitter.com",
            "vxtwitter.com",
            "www.vxtwitter.com",
            "fixupx.com",
            "www.fixupx.com",
            "girlcockx.com",
            "www.girlcockx.com",
        }
    ),
    "youtube": frozenset(
        {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    ),
    "twitch": frozenset({"twitch.tv", "www.twitch.tv", "clips.twitch.tv"}),
    "reddit": frozenset(
        {
            "reddit.com",
            "www.reddit.com",
            "old.reddit.com",
            "new.reddit.com",
            "np.reddit.com",
            "sh.reddit.com",
            "nm.reddit.com",
            "redditmedia.com",
            "www.redditmedia.com",
            "redd.it",
            "i.redd.it",
            "v.redd.it",
            "preview.redd.it",
            "external-preview.redd.it",
        }
    ),
    "threads": frozenset(
        {"threads.net", "www.threads.net", "threads.com", "www.threads.com"}
    ),
    "facebook": frozenset(
        {
            "facebook.com",
            "www.facebook.com",
            "m.facebook.com",
            "web.facebook.com",
            "fb.watch",
        }
    ),
    "pixiv": frozenset({"pixiv.net", "www.pixiv.net"}),
    "bluesky": frozenset({"bsky.app", "www.bsky.app"}),
    "newgrounds": frozenset({"newgrounds.com", "www.newgrounds.com"}),
    "tumblr": frozenset({"tumblr.com", "www.tumblr.com"}),
    "pinterest": frozenset({"pin.it", "pinterest.com", "www.pinterest.com"}),
    "soundcloud": frozenset({"soundcloud.com", "on.soundcloud.com"}),
    "klipy": frozenset({"klipy.com", "www.klipy.com", "static.klipy.com"}),
    "tenor": frozenset(
        {
            "tenor.com",
            "www.tenor.com",
            "tenor.co",
            "media.tenor.com",
            "media1.tenor.com",
            "c.tenor.com",
        }
    ),
}
DISCORD_MEDIA_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
DIRECT_MEDIA_HOSTS = frozenset(
    {
        "static.klipy.com",
        "media.tenor.com",
        "media1.tenor.com",
        "c.tenor.com",
    }
)
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi"})
_MEDIA_SUFFIXES = _IMAGE_SUFFIXES | _VIDEO_SUFFIXES | frozenset({".gif"})
_MEDIA_CONTENT_SUFFIXES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}
_DIRECT_MEDIA_HEADERS = {
    **base_header,
    "Accept": "image/avif,image/webp,image/apng,image/*,video/*;q=0.8,*/*;q=0.5",
}
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_EXTRACTED_MEDIA = 20

# TikTok occasionally changes which mobile app profile its API accepts.  Keep
# a small, known-good fallback list so a transient extractor response does not
# turn into a generic download failure for users.
_TIKTOK_EXTRACTOR_PROFILES: tuple[str, ...] = (
    (
        "tiktok:app_name=musical_ly;app_version=35.1.3;"
        "manifest_app_version=2023501030;aid=1233"
    ),
    (
        "tiktok:app_name=trill;app_version=35.1.3;"
        "manifest_app_version=2023501030;aid=1180"
    ),
)

# YouTube's default client set can intermittently return SABR-only formats
# and the misleading "page needs to be reloaded" error.  Try the embedded
# client first, then clients that can expose HLS or progressive fallbacks.
_YOUTUBE_AUTHENTICATED_CLIENT = "youtube:player_client=web_embedded,web_safari,mweb"

_COOKIE_MAP: list[tuple[Any, str]] = [
    (YOUTUBE_RE, str(FILES_ROOT / "cookies" / "youtube-cookies.txt")),
    (YT_SHORT_RE, str(FILES_ROOT / "cookies" / "youtube-cookies.txt")),
    (YT_CLIP_RE, str(FILES_ROOT / "cookies" / "youtube-cookies.txt")),
    (TWITTER_RE, str(FILES_ROOT / "cookies" / "twitter-cookies.txt")),
    (INSTAGRAM_RE, str(FILES_ROOT / "cookies" / "instagram-cookies.txt")),
]

# Some clients use these mirrors to share Instagram and TikTok links.  They
# expose the same post paths, so canonicalize them before regex matching,
# host allow-list validation, yt-dlp invocation, and download statistics.
_DOWNLOAD_HOST_ALIASES: dict[str, str] = {
    "kkinstagram.com": "instagram.com",
    "www.kkinstagram.com": "www.instagram.com",
    "kktiktok.com": "tiktok.com",
    "www.kktiktok.com": "www.tiktok.com",
}


def normalize_download_url(url: str) -> str:
    """Return the canonical source URL for supported mirror domains.

    The path, query string, and fragment are preserved exactly.  Invalid or
    non-HTTP values are returned untouched so callers can perform their normal
    validation and report the appropriate error.
    """
    if not isinstance(url, str):
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    hostname = (parsed.hostname or "").lower().rstrip(".")
    replacement = _DOWNLOAD_HOST_ALIASES.get(hostname)
    if replacement is None:
        return url
    return parsed._replace(netloc=replacement).geturl()


def _canonical_download_hostname(hostname: str) -> str:
    hostname = hostname.lower().rstrip(".")
    return _DOWNLOAD_HOST_ALIASES.get(hostname, hostname)


def _get_cookies(url: str) -> Optional[str]:
    url = normalize_download_url(url)
    for pattern, path in _COOKIE_MAP:
        if pattern.search(url) and os.path.isfile(path):
            return path
    return None


def is_discord_media_url(url: str) -> bool:
    """Return whether *url* points to a Discord-hosted attachment."""
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return hostname in DISCORD_MEDIA_HOSTS


def _is_allowed_download_host(hostname: str) -> bool:
    """Return whether a page host is supported by the downloader.

    Tumblr blogs use arbitrary subdomains (``blog-name.tumblr.com``), so an
    exact host set cannot cover them without allowing unrelated domains.
    Restrict that exception to one label directly below tumblr.com.
    """
    hostname = _canonical_download_hostname(hostname)
    return hostname in DOWNLOAD_HOSTS or (
        hostname.endswith(".tumblr.com") and hostname.count(".") == 2
    )


def _page_allowed_hosts(hostname: str) -> frozenset[str]:
    """Build the redirect allowlist for a supported source page."""
    hostname = _canonical_download_hostname(hostname)
    if hostname.endswith(".tumblr.com") and hostname.count(".") == 2:
        return frozenset({hostname, "tumblr.com", "www.tumblr.com"})
    site = normalize_download_site(f"https://{hostname}/")
    if site is not None:
        return frozenset(_DOWNLOAD_SITE_HOSTS[site] | {hostname})
    return frozenset({hostname})


def normalize_download_site(url: str) -> str | None:
    """Return a stable site label without retaining the source URL."""
    url = normalize_download_url(url)
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    if hostname.endswith(".tumblr.com") and hostname.count(".") == 2:
        return "tumblr"
    for site, hosts in _DOWNLOAD_SITE_HOSTS.items():
        if hostname in hosts:
            return site
    return None


async def record_download(
    ctx: "Context", url: str, *, auto_download: bool = False
) -> None:
    """Record one successful download while respecting the user's opt-out."""
    site = normalize_download_site(url)
    if site is None or ctx.bot.db_cache.user_tracking_opted_out(
        ctx.author.id, "downloads"
    ):
        return

    try:
        await ctx.bot.pool.execute(
            """
            INSERT INTO download_stats (user_id, site, auto_download, downloads)
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (user_id, site, auto_download) DO UPDATE
            SET downloads = download_stats.downloads + 1,
                last_downloaded_at = now()
            """,
            ctx.author.id,
            site,
            auto_download,
        )
        guild = getattr(ctx, "guild", None)
        if guild is not None:
            channel = getattr(ctx, "channel", None)
            await ctx.bot.pool.execute(
                """
                INSERT INTO download_events (
                    user_id, guild_id, channel_id, site, auto_download
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                ctx.author.id,
                guild.id,
                getattr(channel, "id", None),
                site,
                auto_download,
            )
    except Exception:
        ctx.bot.logger.exception("Failed to record download site statistics")


def is_downloadable_media_page(url: str) -> bool:
    """Return whether a URL should use the guarded yt-dlp workflow."""
    url = normalize_download_url(url)
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or not _is_allowed_download_host(
        hostname
    ):
        return False
    suffix = os.path.splitext(parsed.path)[1].casefold()
    return suffix not in {
        ".gif",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".mp4",
        ".webm",
        ".mov",
        ".mp3",
        ".wav",
        ".ogg",
    }


def download_format_selector(
    url: str,
    output_format: str,
    *,
    res_target: int,
) -> str:
    """Choose a yt-dlp format without rejecting X GIFs with unknown dimensions."""
    url = normalize_download_url(url)
    if SOUNDCLOUD_RE.search(url) or output_format == "mp3":
        return "bestaudio/best"
    if INSTAGRAM_RE.search(url):
        return "bestvideo+bestaudio/best"

    selector = (
        f"bestvideo[height<={res_target}]+bestaudio/" f"best[height<={res_target}]"
    )
    if (
        TWITTER_RE.search(url)
        or TIKTOK_RE.search(url)
        or REDDIT_RE.search(url)
        or FACEBOOK_RE.search(url)
        or TUMBLR_RE.search(url)
    ):
        # X and TikTok sometimes expose a single progressive format whose
        # height is unknown to yt-dlp, or only expose a height above our
        # preferred target. Without this fallback the height filter removes
        # the post's only downloadable format.
        selector += "/best"
    return selector


def _summarize_yt_dlp_error(stderr: str) -> str:
    """Return a short, URL-redacted diagnostic suitable for the bot log."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "no stderr output"
    detail = " | ".join(lines[-3:])
    detail = re.sub(r"https?://\S+", "<url>", detail)
    return detail[:1000]


def _youtube_unavailable_message(stderr: str) -> str | None:
    """Return a useful message for YouTube videos blocked by a rights claim.

    yt-dlp reports a copyright block on stderr, but the downloader used to
    discard that detail and return the same generic error used for transient
    extractor failures.  A rights claim is not recoverable by retrying or by
    changing the requested format, so tell the user why this particular URL
    cannot be downloaded.
    """
    if re.search(r"blocked due to (?:the )?claimed content", stderr, re.I):
        return (
            "YouTube blocked this video because of a copyright claim, so it "
            "is not available for download."
        )
    return None


def _is_video_file(path: str) -> bool:
    return os.path.splitext(path)[1].casefold() in _VIDEO_SUFFIXES


def _needs_discord_mp4(path: str) -> bool:
    """Return whether a downloaded video needs container normalization."""
    return _is_video_file(path) and os.path.splitext(path)[1].casefold() != ".mp4"


def _is_gallery_file(path: str) -> bool:
    extension = os.path.splitext(path)[1].casefold()
    # Keep formats that Discord reliably renders in a media gallery.  Other
    # video containers are still valid downloads, but should be sent as a
    # normal attachment or URL instead of being placed in an embed-like
    # gallery component.
    return extension in _IMAGE_SUFFIXES or extension in {".gif", ".mp4"}


def _normalise_extracted_url(value: object, base_url: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = html_lib.unescape(value).replace("\\/", "/").strip()
    if not value or value.startswith(("data:", "blob:")):
        return None
    candidate = urljoin(base_url, value)
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return candidate


def _extract_html_media_urls(
    text: str,
    base_url: str,
    *,
    include_images_with_video: bool = False,
) -> list[str]:
    """Extract post media from OpenGraph, video tags, and JSON fragments."""
    soup = BeautifulSoup(text, "html.parser")
    videos: list[str] = []
    images: list[str] = []

    def add(target: list[str], value: object) -> None:
        candidate = _normalise_extracted_url(value, base_url)
        if candidate and candidate not in target:
            target.append(candidate)

    for tag in soup.find_all(("video", "source")):
        add(videos, tag.get("src"))
        add(videos, tag.get("data-src"))

    video_meta = {
        "og:video",
        "og:video:url",
        "og:video:secure_url",
        "twitter:player:stream",
    }
    image_meta = {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}
    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content")
        if key in video_meta:
            add(videos, content)
        elif key in image_meta:
            add(images, content)

    if not videos and not images:
        for link in soup.find_all("link"):
            rel = {str(item).lower() for item in (link.get("rel") or [])}
            if "image_src" in rel:
                add(images, link.get("href"))
        for tag in soup.find_all("img"):
            add(images, tag.get("src"))
            add(images, tag.get("data-src"))

    # Some server-rendered pages only expose the CDN URL inside escaped JSON.
    # Decode the JSON-style escaped slashes before scanning.  Otherwise a URL
    # such as ``https:\/\/cdn.example/video.mp4`` is invisible to the regex.
    # Only use this fallback when the page has no media tags.  A page with an
    # OpenGraph thumbnail can also contain dozens of unrelated loading images.
    if not videos and not images:
        searchable_text = html_lib.unescape(text).replace("\\/", "/")
        escaped_urls = re.findall(
            r"https?://[^\"'<>\\\s]+?\.(?:mp4|webm|mov|gif|jpe?g|png|webp)(?:\?[^\"'<>\\\s]*)?",
            searchable_text,
            re.IGNORECASE,
        )
        for value in escaped_urls:
            add(
                videos if re.search(r"\.(?:mp4|webm|mov)$", value, re.I) else images,
                value,
            )

    extracted = videos + images if include_images_with_video else (videos or images)
    return extracted[:_MAX_EXTRACTED_MEDIA]


def _is_threads_media_url(value: str) -> bool:
    """Only accept media hosted on Meta's public CDN domains."""
    hostname = (urlsplit(value).hostname or "").lower().rstrip(".")
    return hostname.endswith(".cdninstagram.com") or hostname.endswith(".fbcdn.net")


def _extract_threads_json_media_urls(text: str, base_url: str) -> list[str]:
    """Extract each attachment from Threads' embedded post payload.

    Threads' server-rendered page stores carousel attachments in JSON script
    tags.  The normal OpenGraph tags only expose a thumbnail, while scanning
    every URL in the page also picks up profile pictures and related posts.
    Walking the first carousel payload lets us select one best URL per
    attachment without downloading unrelated assets.
    """
    soup = BeautifulSoup(text, "html.parser")

    def first_candidate(item: object) -> str | None:
        if not isinstance(item, dict):
            return None
        versions = item.get("video_versions")
        if isinstance(versions, list):
            for version in versions:
                if not isinstance(version, dict):
                    continue
                candidate = _normalise_extracted_url(version.get("url"), base_url)
                if candidate and _is_threads_media_url(candidate):
                    return candidate
        image_data = item.get("image_versions2")
        candidates = (
            image_data.get("candidates") if isinstance(image_data, dict) else None
        )
        if isinstance(candidates, list):
            for version in candidates:
                if not isinstance(version, dict):
                    continue
                candidate = _normalise_extracted_url(version.get("url"), base_url)
                if candidate and _is_threads_media_url(candidate):
                    return candidate
        return None

    def walk(value: object, depth: int = 0) -> list[str]:
        if depth > 16:
            return []
        if isinstance(value, dict):
            carousel = value.get("carousel_media")
            if isinstance(carousel, list):
                media = [
                    candidate
                    for item in carousel
                    if (candidate := first_candidate(item))
                ]
                if media:
                    return list(dict.fromkeys(media))[:_MAX_EXTRACTED_MEDIA]
            candidate = first_candidate(value)
            if candidate:
                return [candidate]
            for child in value.values():
                media = walk(child, depth + 1)
                if media:
                    return media
        elif isinstance(value, list):
            for child in value:
                media = walk(child, depth + 1)
                if media:
                    return media
        return []

    for script in soup.find_all("script", type="application/json"):
        payload = script.string or script.get_text()
        if not payload:
            continue
        try:
            value = json.loads(payload)
        except (TypeError, ValueError):
            continue
        media = walk(value)
        if media:
            return media
    return []


# ffmpeg filter for optimized GIF conversion:
#   10 fps, 480px wide (AR preserved), lanczos scaling,
#   palettegen with diff mode + 128 colors,
#   paletteuse with Bayer ordered dithering (smaller output than Floyd-Steinberg).
GIF_FILTER: str = (
    "fps=10,scale=480:-1:flags=lanczos,"
    "split[s0][s1];"
    "[s0]palettegen=max_colors=128:stats_mode=diff[p];"
    "[s1][p]paletteuse=dither=bayer:bayer_scale=5"
)


class Downloader:
    def __init__(
        self,
        ctx: Context,
        url: str,
        format: str = "mp4",
        filename: Optional[str] = None,
        hidden: bool = False,
        auto_download: bool = False,
        allow_temporary_hosting: bool = False,
        ignore_checks: bool = False,
    ) -> None:
        self.ctx = ctx
        self.url = normalize_download_url(url)
        self.format = format
        self.auto_download = auto_download
        self.allow_temporary_hosting = allow_temporary_hosting
        self.ignore_checks = ignore_checks
        raw_filename = filename or secrets.token_urlsafe(8).strip("-")
        # Titles are user-controlled (``download --title``).  Restrict the
        # name to a plain filename so it cannot escape the download directory
        # or inject shell syntax into cleanup/logging paths.
        safe_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw_filename))
        self.filename = safe_filename.strip("._")[:80] or secrets.token_urlsafe(
            8
        ).strip("-")
        self.hidden = hidden
        self.max_filesize = (
            ctx.guild.filesize_limit
            if ctx.guild is not None
            else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        )
        self._duration = 0
        self._deadline: float | None = None
        self._job_dir: str | None = None
        self._cookie_file: str | None = None

    @property
    def upload_limit_description(self) -> str:
        size_mb = self.max_filesize / (1024 * 1024)
        scope = "this server's" if self.ctx.guild is not None else "Discord's"
        return f"{scope} {size_mb:g} MB upload limit"

    def _remaining_timeout(self) -> float:
        if self._deadline is None:
            return DOWNLOAD_TIMEOUT
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise DownloadError(
                f"This download took longer than {DOWNLOAD_TIMEOUT_MINUTES} minutes "
                "and was stopped. "
                "Try a shorter video."
            )
        return remaining

    async def _communicate_with_timeout(
        self, proc: Any, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        """Collect subprocess output while ensuring child processes cannot linger."""
        if timeout is None:
            try:
                timeout = self._remaining_timeout()
            except DownloadError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.communicate()
                raise
        communicate_task = asyncio.create_task(proc.communicate())
        try:
            return await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await communicate_task
            raise DownloadError(
                f"This download took longer than {DOWNLOAD_TIMEOUT_MINUTES} minutes "
                "and was stopped. "
                "Try a shorter video."
            ) from exc
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await communicate_task
            raise

    def _output_glob(self) -> str:
        if self._job_dir is None:
            raise DownloadError("The download job was not initialized.")
        return os.path.join(self._job_dir, f"{self.filename}.*")

    @staticmethod
    def _sort_output_paths(matches: list[str]) -> list[str]:
        def sort_key(path: str) -> tuple[int, str]:
            # Playlist entries use ``.001.``, ``.002.``, and so on. A single
            # item may use yt-dlp's ``NA`` placeholder.
            match = re.search(r"\.(\d+)\.[^.]+$", path)
            return (int(match.group(1)), path) if match else (10**9, path)

        return sorted(matches, key=sort_key)

    @staticmethod
    def _output_entry_key(path: str) -> str:
        match = re.search(r"\.(\d+|NA)\.[^.]+$", path)
        return match.group(1) if match else path

    def _find_outputs(self, pattern: str | None = None) -> list[str]:
        matches = [
            path
            for path in glob.glob(pattern or self._output_glob())
            if os.path.isfile(path)
        ]
        if not matches:
            raise DownloadError("Download completed but no output file was found.")
        return self._sort_output_paths(matches)

    def _find_output(self) -> str:
        """Return the first output for callers that only need one item."""
        return self._find_outputs()[0]

    def _cleanup_output(self) -> None:
        for path in glob.glob(self._output_glob()):
            try:
                os.remove(path)
            except OSError:
                pass

    def _prepare_cookie_file(self, source: str) -> str:
        """Copy read-only site cookies into this download's private job directory."""
        if self._cookie_file is not None and os.path.isfile(self._cookie_file):
            return self._cookie_file
        if self._job_dir is None:
            raise DownloadError("The download job was not initialized.")

        cookie_file = os.path.join(self._job_dir, ".yt-dlp-cookies.txt")
        try:
            shutil.copyfile(source, cookie_file)
            os.chmod(cookie_file, 0o600)
        except OSError as exc:
            raise DownloadError(
                "Could not prepare site cookies for this download."
            ) from exc

        self._cookie_file = cookie_file
        return cookie_file

    async def _yt_dlp_download(self, video: str, *, res_target: int) -> list[str]:
        """Download every media entry returned by yt-dlp for a post."""

        is_youtube = bool(YOUTUBE_RE.search(video) or YT_SHORT_RE.search(video))
        is_audio = SOUNDCLOUD_RE.search(video) or self.format == "mp3"
        is_klipy = bool(KLIPY_RE.search(video))
        is_instagram = bool(INSTAGRAM_RE.search(video))
        is_tiktok = bool(TIKTOK_RE.search(video))
        is_reddit = bool(REDDIT_RE.search(video))
        is_tumblr = bool(TUMBLR_RE.search(video))
        is_multi_post = (
            is_instagram or bool(TWITTER_RE.search(video)) or is_reddit or is_tumblr
        )
        is_instagram_post = is_instagram and "/p/" in urlsplit(video).path

        format_selector = download_format_selector(
            video,
            self.format,
            res_target=res_target,
        )

        args = [
            sys.executable,
            "-m",
            "utils.ytdlp_safe",
            "-f",
            format_selector,
            "-o",
            os.path.join(
                self._job_dir or "",
                f"{self.filename}.%(playlist_index)03d.%(ext)s",
            ),
            "--yes-playlist" if is_multi_post else "--no-playlist",
            "--js-runtimes",
            "deno",
            "--retries",
            "2",
            "--fragment-retries",
            "2",
            "--print",
            "after_move:filepath",
        ]

        tiktok_extractor_arg_index: int | None = None
        cookie_arg_index: int | None = None

        if cookies := _get_cookies(video):
            cookie_arg_index = len(args)
            args += ["--cookies", self._prepare_cookie_file(cookies)]

        if is_youtube:
            args += ["--extractor-args", _YOUTUBE_AUTHENTICATED_CLIENT]
            provider_url = os.environ.get("FISHIE_YOUTUBE_POT_PROVIDER_URL", "").strip()
            if provider_url:
                args += [
                    "--extractor-args",
                    f"youtubepot-bgutilhttp:base_url={provider_url.rstrip('/')}",
                ]

        # Instagram returns different DASH manifests over this host's IPv6
        # route for some Reels.  The IPv4 response includes the matching audio
        # adaptation set that Discord needs for proper playback.
        if is_instagram:
            args.append("--force-ipv4")
            if not is_audio:
                args += ["--format-sort", f"res:{res_target}"]

        if is_tiktok:
            # TikTok's web endpoint intermittently serves an empty challenge
            # page. The mobile extractor is more reliable when it receives a
            # current, explicit app profile instead of choosing one at random.
            tiktok_extractor_arg_index = len(args) + 1
            args += [
                "--extractor-args",
                _TIKTOK_EXTRACTOR_PROFILES[0],
                "--force-ipv4",
            ]

        if not is_youtube:
            args += [
                "--add-header",
                "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "--add-header",
                "Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "--add-header",
                "Accept-Language:en-US,en;q=0.9",
            ]

        if is_klipy:
            # Klipy's page extractor can be challenged by Cloudflare. yt-dlp
            # uses curl_cffi to impersonate a browser for the generic fallback.
            args += ["--extractor-args", "generic:impersonate"]

        if is_audio:
            args += ["--extract-audio", "--audio-format", "mp3"]
            self.format = "mp3"

        # Reject live broadcasts before yt-dlp starts consuming an endless
        # stream. This also catches YouTube watch URLs that resolve to live
        # content while leaving regular recorded videos available.
        args += ["--break-match-filters", "!is_live"]

        args.append(video)

        # Instagram can reject an otherwise valid public post with HTTP 400
        # when the stored session cookie is stale or incompatible with the
        # current web API. Retry once without that cookie so public posts can
        # use yt-dlp's logged-out extractor. Private/login-only posts still
        # fail normally after the anonymous attempt.
        attempts = len(_TIKTOK_EXTRACTOR_PROFILES) if is_tiktok else 1
        if is_instagram and cookie_arg_index is not None:
            attempts = 2
        last_returncode: int | None = None
        last_stderr = ""
        last_failure = ""
        downloaded_paths: list[str] = []

        for attempt in range(attempts):
            if attempt:
                self._cleanup_output()
                if is_instagram and cookie_arg_index is not None:
                    del args[cookie_arg_index : cookie_arg_index + 2]
                    cookie_arg_index = None
                    self.ctx.bot.logger.info(
                        "Retrying Instagram download without stored cookies host=%s",
                        urlsplit(video).hostname,
                    )
                elif tiktok_extractor_arg_index is not None:
                    args[tiktok_extractor_arg_index] = _TIKTOK_EXTRACTOR_PROFILES[
                        attempt
                    ]
                # Give TikTok's edge endpoint a short opportunity to recover,
                # without extending the downloader's single global deadline.
                await asyncio.sleep(min(0.75, self._remaining_timeout()))

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr_raw = await self._communicate_with_timeout(proc)
            except DownloadError:
                self._cleanup_output()
                raise

            last_returncode = proc.returncode
            last_stderr = stderr_raw.decode(errors="replace").strip()

            try:
                output_paths = self._find_outputs()
            except DownloadError:
                output_paths = []
            if output_paths:
                downloaded_paths = output_paths

            if proc.returncode != 0:
                if "is_live" in last_stderr and "filter" in last_stderr:
                    self._cleanup_output()
                    raise DownloadError(
                        "Live streams cannot be downloaded. Please provide a recorded video."
                    )
                last_failure = "yt-dlp exited before producing a media file."
                if attempt + 1 < attempts:
                    self.ctx.bot.logger.warning(
                        "yt-dlp attempt failed host=%s attempt=%s/%s; retrying",
                        urlsplit(video).hostname,
                        attempt + 1,
                        attempts,
                    )
                    continue
            else:
                if output_paths:
                    if not is_instagram_post:
                        return output_paths
                    break
                last_failure = "yt-dlp completed without producing a media file."
                if attempt + 1 < attempts:
                    self.ctx.bot.logger.warning(
                        "yt-dlp produced no output host=%s attempt=%s/%s; retrying",
                        urlsplit(video).hostname,
                        attempt + 1,
                        attempts,
                    )
                    continue

            break

        # Instagram carousels can mix videos and images. The normal format
        # download produces the video entries, while this thumbnail pass fills
        # in any image entries whose extractor reports no video formats.
        if is_instagram_post:
            thumbnail_paths = await self._instagram_thumbnail_download(video)
            if thumbnail_paths:
                outputs_by_entry = {
                    self._output_entry_key(path): path for path in downloaded_paths
                }
                for path in thumbnail_paths:
                    outputs_by_entry.setdefault(self._output_entry_key(path), path)
                downloaded_paths = self._sort_output_paths(
                    list(outputs_by_entry.values())
                )
            if downloaded_paths:
                return downloaded_paths

        # Instagram photo posts (and carousels whose first item is a photo)
        # have no video formats. yt-dlp can still retrieve the public post
        # image as a thumbnail, so use that before returning the generic video
        # error. This keeps the same path usable by the normal downloader,
        # auto-download, and media effects.
        if is_instagram and re.search(
            r"no video|no video formats|requested format is not available",
            last_stderr,
            re.IGNORECASE,
        ):
            thumbnail_paths = await self._instagram_thumbnail_download(video)
            if thumbnail_paths is not None:
                return thumbnail_paths

        if is_youtube and (
            blocked_message := _youtube_unavailable_message(last_stderr)
        ):
            self._cleanup_output()
            self.ctx.bot.logger.warning(
                "YouTube rejected the video because of a claimed-content block "
                "host=%s",
                urlsplit(video).hostname,
            )
            raise DownloadError(blocked_message)

        self.ctx.bot.logger.error(
            "yt-dlp failed exit=%s host=%s attempts=%s detail=%s",
            last_returncode,
            urlsplit(video).hostname,
            attempts,
            _summarize_yt_dlp_error(last_stderr),
        )
        if last_failure:
            self.ctx.bot.logger.error(last_failure)
        raise DownloadError(
            "Failed to download. The site may be blocking "
            "the request, or the video may be unavailable."
        )

    async def _instagram_thumbnail_download(self, video: str) -> list[str] | None:
        """Download every public Instagram image entry as a thumbnail."""
        thumbnail_glob = os.path.join(
            self._job_dir or "", f"{self.filename}.thumbnail.*"
        )
        args = [
            sys.executable,
            "-m",
            "utils.ytdlp_safe",
            "--yes-playlist",
            "--force-ipv4",
            "--ignore-no-formats-error",
            "--skip-download",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "-o",
            os.path.join(
                self._job_dir or "",
                f"{self.filename}.thumbnail.%(playlist_index)03d.%(ext)s",
            ),
        ]

        cookie_arg_index: int | None = None
        if cookies := _get_cookies(video):
            cookie_arg_index = len(args)
            args += ["--cookies", self._prepare_cookie_file(cookies)]

        args += [
            "--add-header",
            "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
            "Safari/537.36",
            "--add-header",
            "Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "--add-header",
            "Accept-Language:en-US,en;q=0.9",
            video,
        ]

        attempts = 2 if cookie_arg_index is not None else 1
        last_detail = "no stderr output"
        for attempt in range(attempts):
            if attempt:
                for path in glob.glob(thumbnail_glob):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                if cookie_arg_index is not None:
                    del args[cookie_arg_index : cookie_arg_index + 2]
                    cookie_arg_index = None
                    self.ctx.bot.logger.info(
                        "Retrying Instagram thumbnail lookup without stored cookies "
                        "host=%s",
                        urlsplit(video).hostname,
                    )

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                _, stderr_raw = await self._communicate_with_timeout(proc)
            except DownloadError:
                for path in glob.glob(thumbnail_glob):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                raise

            last_detail = _summarize_yt_dlp_error(stderr_raw.decode(errors="replace"))
            if proc.returncode != 0:
                continue

            try:
                paths = [
                    path
                    for path in self._find_outputs(thumbnail_glob)
                    if os.path.splitext(path)[1].casefold() in _IMAGE_SUFFIXES
                ]
            except DownloadError:
                paths = []
            if paths:
                return paths

        for path in glob.glob(thumbnail_glob):
            try:
                os.remove(path)
            except OSError:
                pass
        self.ctx.bot.logger.warning(
            "Instagram thumbnail fallback failed host=%s detail=%s",
            urlsplit(video).hostname,
            last_detail,
        )
        return None

    async def _fetch_page_html(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str:
        """Fetch a supported public page while validating every redirect."""
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        allowed_hosts = _page_allowed_hosts(hostname)
        current = url
        request_headers = {
            **base_header,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            **(headers or {}),
        }

        try:
            async with asyncio.timeout(self._remaining_timeout()):
                for _ in range(6):
                    await validate_public_url(
                        current,
                        allowed_hosts=allowed_hosts,
                        allow_http=False,
                    )
                    async with self.ctx.session.get(
                        current,
                        headers=request_headers,
                        allow_redirects=False,
                    ) as response:
                        validate_connected_peer(response)
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                raise DownloadError(
                                    "The source page returned an invalid redirect."
                                )
                            current = urljoin(current, location)
                            continue
                        if response.status != 200:
                            raise DownloadError(
                                f"The source page returned HTTP {response.status}."
                            )
                        content_type = response.headers.get("Content-Type", "").lower()
                        if content_type and not content_type.startswith(
                            ("text/html", "application/xhtml+xml")
                        ):
                            raise DownloadError("The source page did not return HTML.")
                        data = await read_bounded_response(response, _MAX_PAGE_BYTES)
                        return data.decode("utf-8", errors="replace")
        except DownloadError:
            raise
        except commands.CommandError as exc:
            raise DownloadError("The source page could not be fetched.") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DownloadError("The source page could not be fetched.") from exc

        raise DownloadError("The source page redirected too many times.")

    @staticmethod
    def _pixiv_artwork_id(url: str) -> str | None:
        match = re.search(
            r"(?:/artworks/|illust_id=)(?P<id>\d+)",
            url,
            re.IGNORECASE,
        )
        return match.group("id") if match else None

    async def _pixiv_media_urls(self, url: str) -> list[str]:
        """Resolve every page of a public Pixiv illustration."""
        artwork_id = self._pixiv_artwork_id(url)
        if artwork_id is None:
            return []

        api_url = f"https://www.pixiv.net/ajax/illust/{artwork_id}/pages?lang=en"
        headers = {
            **base_header,
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"https://www.pixiv.net/artworks/{artwork_id}",
        }
        try:
            await validate_public_url(
                api_url,
                allowed_hosts={"www.pixiv.net", "pixiv.net"},
                allow_http=False,
            )
            async with asyncio.timeout(self._remaining_timeout()):
                async with self.ctx.session.get(
                    api_url,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    validate_connected_peer(response)
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                    else:
                        payload = None
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
            commands.CommandError,
        ):
            payload = None

        results: list[str] = []
        body = payload.get("body") if isinstance(payload, dict) else None
        if isinstance(body, list):
            for item in body:
                original = (
                    item.get("urls", {}).get("original")
                    if isinstance(item, dict)
                    else None
                )
                parsed = urlsplit(original) if isinstance(original, str) else None
                if (
                    isinstance(original, str)
                    and parsed
                    and parsed.scheme == "https"
                    and parsed.hostname in {"i.pximg.net", "s.pximg.net"}
                    and original not in results
                ):
                    results.append(original)

        if results:
            return results[:_MAX_EXTRACTED_MEDIA]

        # Pixiv occasionally denies the AJAX request to an unauthenticated
        # client. The public page still includes an OpenGraph image in that
        # case, which at least preserves single-image downloads.
        try:
            page = await self._fetch_page_html(url)
        except DownloadError:
            return []
        return [
            candidate
            for candidate in _extract_html_media_urls(page, url)
            if (urlsplit(candidate).hostname or "").lower()
            in {"i.pximg.net", "s.pximg.net"}
        ][:_MAX_EXTRACTED_MEDIA]

    async def _threads_share_redirect_url(self, url: str) -> str:
        """Resolve a Threads ``/share/<token>`` URL to its post URL.

        Share links return the normal Threads application shell unless the
        private ``__a=1`` response is requested.  That response is still
        public and contains a short-lived, same-host redirect to the post.
        """
        parsed = urlsplit(url)
        if "/share/" not in parsed.path.lower():
            return url

        query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "__a"]
        query.append(("__a", "1"))
        resolver_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query),
                parsed.fragment,
            )
        )
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            await validate_public_url(
                resolver_url,
                allowed_hosts=_page_allowed_hosts(hostname),
                allow_http=False,
            )
            async with asyncio.timeout(self._remaining_timeout()):
                async with self.ctx.session.get(
                    resolver_url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/x-javascript,application/json;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    allow_redirects=False,
                ) as response:
                    validate_connected_peer(response)
                    if response.status != 200:
                        return url
                    body = await read_bounded_response(response, 128 * 1024)
            raw = body.decode("utf-8", errors="replace").removeprefix("for (;;);")
            payload = json.loads(raw)
            redirect = payload.get("redirect") if isinstance(payload, dict) else None
            candidate = _normalise_extracted_url(redirect, url)
            if not candidate:
                return url
            await validate_public_url(
                candidate,
                allowed_hosts=_page_allowed_hosts(hostname),
                allow_http=False,
            )
            return candidate
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            UnicodeError,
            ValueError,
            commands.CommandError,
        ):
            return url

    async def _threads_oembed_media_urls(self, url: str | None = None) -> list[str]:
        """Use Threads' public oEmbed response when the post HTML is sparse."""
        endpoint = "https://graph.threads.net/oembed"
        target_url = url or self.url
        try:
            await validate_public_url(
                endpoint,
                allowed_hosts={"graph.threads.net"},
                allow_http=False,
            )
            async with asyncio.timeout(self._remaining_timeout()):
                async with self.ctx.session.get(
                    endpoint,
                    params={"url": target_url},
                    headers={**base_header, "Accept": "application/json"},
                    allow_redirects=False,
                ) as response:
                    validate_connected_peer(response)
                    if response.status != 200:
                        return []
                    payload = await response.json(content_type=None)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
            commands.CommandError,
        ):
            return []

        if not isinstance(payload, dict):
            return []
        media: list[str] = []
        embedded_html = payload.get("html")
        if isinstance(embedded_html, str):
            media.extend(
                _extract_html_media_urls(
                    embedded_html,
                    target_url,
                    include_images_with_video=True,
                )
            )
        for key in (
            "media_url",
            "video_url",
            "image_url",
            "thumbnail_url",
        ):
            candidate = _normalise_extracted_url(payload.get(key), target_url)
            if candidate and candidate not in media:
                media.append(candidate)
        return media[:_MAX_EXTRACTED_MEDIA]

    async def _download_site_media(self) -> list[str] | None:
        """Resolve sites without a maintained yt-dlp extractor."""
        if PIXIV_RE.search(self.url):
            media_urls = await self._pixiv_media_urls(self.url)
            referer = "https://www.pixiv.net/"
        elif THREADS_RE.search(self.url):
            resolved_url = await self._threads_share_redirect_url(self.url)
            try:
                page = await self._fetch_page_html(
                    resolved_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            except DownloadError:
                page = ""
            media_urls = _extract_threads_json_media_urls(page, resolved_url)
            if not media_urls:
                media_urls = _extract_html_media_urls(page, resolved_url)
            if not media_urls:
                media_urls = await self._threads_oembed_media_urls(resolved_url)
            referer = resolved_url
        else:
            return None

        if not media_urls:
            raise DownloadError("No public media was found on that page.")

        paths: list[str] = []
        for index, media_url in enumerate(media_urls, start=1):
            try:
                path = await self._download_direct_media(
                    media_url,
                    headers={"Referer": referer},
                    output_stem=f"{self.filename}.{index:03d}",
                )
            except DownloadError as exc:
                self.ctx.bot.logger.warning(
                    "Source media item failed host=%s detail=%s",
                    urlsplit(media_url).hostname,
                    str(exc),
                )
                continue
            paths.append(path)
        if not paths:
            raise DownloadError("The source media could not be downloaded.")
        return paths

    async def _download_direct_media(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        output_stem: str | None = None,
    ) -> str:
        """Download a public direct image or video without invoking yt-dlp."""
        if self._job_dir is None:
            raise DownloadError("The download job was not initialized.")

        current = url
        request_headers = {**_DIRECT_MEDIA_HEADERS, **(headers or {})}
        output_path: str | None = None
        try:
            async with asyncio.timeout(self._remaining_timeout()):
                for _ in range(6):
                    await validate_public_url(current, allow_http=False)
                    async with self.ctx.session.get(
                        current,
                        headers=request_headers,
                        allow_redirects=False,
                    ) as response:
                        validate_connected_peer(
                            response,
                            allow_missing_peer=(
                                (urlsplit(current).hostname or "")
                                .casefold()
                                .rstrip(".")
                                in DIRECT_MEDIA_HOSTS
                            ),
                        )
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                raise DownloadError(
                                    "The media server returned an invalid redirect."
                                )
                            current = urljoin(current, location)
                            continue
                        if response.status != 200:
                            raise DownloadError(
                                f"The media server returned HTTP {response.status}."
                            )

                        content_type = (
                            response.headers.get("Content-Type", "")
                            .split(";", 1)[0]
                            .lower()
                        )
                        suffix = os.path.splitext(urlsplit(current).path)[1].lower()
                        if suffix not in _MEDIA_SUFFIXES:
                            suffix = _MEDIA_CONTENT_SUFFIXES.get(content_type, "")
                        if suffix not in _MEDIA_SUFFIXES:
                            raise DownloadError(
                                "The source did not return a supported image or video."
                            )
                        if content_type and not (
                            content_type.startswith(("image/", "video/"))
                            or content_type == "application/octet-stream"
                        ):
                            raise DownloadError(
                                "The source did not return a supported image or video."
                            )

                        stem = output_stem or self.filename
                        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
                        output_path = os.path.join(
                            self._job_dir,
                            f"{stem or self.filename}{suffix}",
                        )
                        content_length = response.content_length
                        if (
                            not self.ignore_checks
                            and content_length is not None
                            and content_length > self.max_filesize
                        ):
                            raise DownloadError(
                                f"This media exceeds {self.upload_limit_description}."
                            )

                        size = 0
                        with open(output_path, "wb") as output:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                size += len(chunk)
                                if not self.ignore_checks and size > self.max_filesize:
                                    raise DownloadError(
                                        f"This media exceeds {self.upload_limit_description}."
                                    )
                                output.write(chunk)
                        return output_path
        except DownloadError:
            if output_path:
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            raise
        except commands.CommandError as exc:
            if output_path:
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            raise DownloadError("The requested media could not be downloaded.") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if output_path:
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            raise DownloadError("The requested media could not be downloaded.") from exc

        raise DownloadError("The media server redirected too many times.")

    async def _convert_to_gif(
        self, input_path: str, *, full_frames: bool = False
    ) -> str:
        """Convert a video file to an optimized GIF via ffmpeg.

        Returns the path to the new ``.gif`` file (the original is removed).
        """
        output_path = input_path.rsplit(".", 1)[0] + ".gif"
        args = [
            "ffmpeg",
            "-i",
            input_path,
            "-vf",
            GIF_FILTER,
        ]
        if full_frames:
            # Klipy's native GIFs sometimes use partial-frame rectangles that
            # Discord renders as corrupted blocks. Encode independent frames
            # so every client receives a complete image for each animation step.
            args += ["-gifflags", "-offsetting-transdiff"]
        args += [
            "-y",
            output_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr_raw = await self._communicate_with_timeout(proc)
        except DownloadError:
            self._cleanup_output()
            try:
                os.remove(input_path)
            except OSError:
                pass
            raise

        if proc.returncode != 0:
            try:
                os.remove(input_path)
            except OSError:
                pass
            stderr = stderr_raw.decode(errors="replace").strip() if stderr_raw else ""
            tail = stderr.rsplit("\n", 1)[-1] if stderr else "unknown error"
            raise DownloadError(f"GIF conversion failed: {tail}")

        try:
            os.remove(input_path)
        except OSError:
            pass
        return output_path

    async def _convert_to_mobile_mp4(self, input_path: str) -> str:
        """Transcode a video to a broadly supported H.264/AAC MP4 format.

        The name is kept for the Instagram compatibility path, but the
        conversion is also used for other containers such as MKV and WebM
        when a default-format file must be sent through temporary hosting.
        Local files keep their requested container, while hosted files use an
        MP4 extension and codec that Discord can render reliably.
        """
        output_path = input_path.rsplit(".", 1)[0] + ".compatible.mp4"
        final_path = input_path.rsplit(".", 1)[0] + ".mp4"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-y",
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr_raw = await self._communicate_with_timeout(proc)
        except DownloadError:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise

        if proc.returncode != 0:
            try:
                os.remove(output_path)
            except OSError:
                pass
            stderr = stderr_raw.decode(errors="replace").strip() if stderr_raw else ""
            tail = stderr.rsplit("\n", 1)[-1] if stderr else "unknown error"
            raise DownloadError(f"Video compatibility conversion failed: {tail}")

        try:
            os.replace(output_path, final_path)
            if input_path != final_path:
                os.remove(input_path)
        except OSError as exc:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise DownloadError("Video compatibility conversion failed.") from exc
        return final_path

    async def _has_audio(self, path: str) -> bool:
        """Return True if *path* contains an audio stream (ffprobe)."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        stdout, _ = await self._communicate_with_timeout(proc)
        return proc.returncode == 0 and b"audio" in stdout

    async def _get_duration(self, path: str) -> float:
        """Return duration in seconds from ffprobe, or 0 on failure."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        stdout, _ = await self._communicate_with_timeout(proc)
        if proc.returncode == 0 and stdout:
            try:
                return float(stdout.decode().strip())
            except ValueError:
                pass
        return 0.0

    def _attachment_filename(self, path: str, index: int, total: int) -> str:
        extension = os.path.splitext(path)[1].casefold() or ".mp4"
        if total == 1:
            return f"{self.filename}{extension}"
        return f"{self.filename}-{index}{extension}"

    async def _download_all(self) -> list[discord.File]:
        parsed_url = urlsplit(self.url)
        hostname = (parsed_url.hostname or "").lower().rstrip(".")
        if not _is_allowed_download_host(hostname):
            raise DownloadError("That website is not supported.")
        allowed_hosts = (
            DOWNLOAD_HOSTS if hostname in DOWNLOAD_HOSTS else frozenset({hostname})
        )
        try:
            await validate_public_url(self.url, allowed_hosts=allowed_hosts)
        except commands.CommandError as exc:
            raise DownloadError(str(exc)) from exc

        if any(pattern.search(self.url) for pattern in LIVE_STREAM_RE):
            raise DownloadError(
                "Live streams cannot be downloaded. Please provide a recorded video."
            )

        is_audio = SOUNDCLOUD_RE.search(self.url) or self.format == "mp3"
        # Klipy's resolver returns a direct static media URL.  Keep track of
        # that host because the original klipy.com page URL is no longer
        # available here after resolution.
        direct_media_host = (
            urlsplit(self.url).scheme == "https"
            and (urlsplit(self.url).hostname or "").casefold().rstrip(".")
            in DIRECT_MEDIA_HOSTS
        )
        is_klipy_media = (
            direct_media_host
            and (urlsplit(self.url).hostname or "").casefold().rstrip(".")
            == "static.klipy.com"
        )
        is_site_media = bool(PIXIV_RE.search(self.url) or THREADS_RE.search(self.url))

        if is_site_media:
            output_paths = await self._download_site_media() or []
        elif direct_media_host:
            output_paths = [await self._download_direct_media(self.url)]
        else:
            output_paths = await self._yt_dlp_download(self.url, res_target=1080)

        if is_audio:
            if (
                not self.ignore_checks
                and not self.allow_temporary_hosting
                and any(
                    os.path.getsize(path) > self.max_filesize for path in output_paths
                )
            ):
                self._cleanup_output()
                raise DownloadError(
                    f"This audio file exceeds {self.upload_limit_description}."
                )
            return [
                discord.File(
                    path,
                    filename=self._attachment_filename(path, index, len(output_paths)),
                )
                for index, path in enumerate(output_paths, start=1)
            ]

        if not self.ignore_checks and any(
            os.path.getsize(path) > self.max_filesize for path in output_paths
        ):
            if is_klipy_media or is_site_media:
                if not self.allow_temporary_hosting:
                    self._cleanup_output()
                    raise DownloadError(
                        f"This media exceeds {self.upload_limit_description}."
                    )
            else:
                self._cleanup_output()
                output_paths = await self._yt_dlp_download(self.url, res_target=720)

            if (
                not self.ignore_checks
                and not self.allow_temporary_hosting
                and any(
                    os.path.getsize(path) > self.max_filesize for path in output_paths
                )
            ):
                self._cleanup_output()
                raise DownloadError(
                    f"This video exceeds {self.upload_limit_description} even at "
                    "720p. Try a shorter clip."
                )

        # Instagram may return VP9 video in an MP4 container. Desktop players
        # can decode it, but Discord mobile can display only the first frame.
        # Normalize Instagram downloads to H.264/AAC with fast-start metadata.
        # Preserve an explicitly requested container.  The compatibility
        # transcode is only the default MP4 behavior, so ``-format webm`` (or
        # another caller-selected format) is not silently changed.
        if self.format == "mp4" and INSTAGRAM_RE.search(self.url):
            for index, output_path in enumerate(output_paths):
                if _is_video_file(output_path):
                    output_paths[index] = await self._convert_to_mobile_mp4(output_path)

        # Twitter GIF conversion:
        #   --format gif          → always convert (rejected if > 30s)
        #   --format mp4 (default) → auto-convert if file has no audio track
        if TWITTER_RE.search(self.url):
            requested_format = self.format
            for index, output_path in enumerate(output_paths):
                if not _is_video_file(output_path):
                    continue
                self._duration = await self._get_duration(output_path)
                has_audio = await self._has_audio(output_path)
                want_gif = requested_format == "gif" or (
                    requested_format == "mp4" and not has_audio
                )
                if not want_gif:
                    continue
                if self._duration > 30:
                    raise DownloadError(
                        "GIF conversion is limited to videos 30 seconds or shorter. "
                        "This video is {:.0f} seconds.".format(self._duration)
                    )
                output_paths[index] = await self._convert_to_gif(output_path)

        # Klipy pages represent GIFs as video files.  Normalize those files to
        # an actual GIF before sending so Discord renders them as animated GIFs
        # instead of a video attachment.
        if (
            is_klipy_media
            and not is_audio
            and not output_paths[0].lower().endswith(".gif")
        ):
            output_path = output_paths[0]
            dur = await self._get_duration(output_path)
            if dur > 30:
                self._cleanup_output()
                raise DownloadError(
                    "GIF conversion is limited to videos 30 seconds or shorter. "
                    "This video is {:.0f} seconds.".format(dur)
                )
            output_paths[0] = await self._convert_to_gif(output_path, full_frames=True)

        if (
            not self.ignore_checks
            and not self.allow_temporary_hosting
            and any(os.path.getsize(path) > self.max_filesize for path in output_paths)
        ):
            self._cleanup_output()
            raise DownloadError(
                f"The downloaded media exceeds {self.upload_limit_description}."
            )

        total = len(output_paths)
        return [
            discord.File(
                path,
                filename=self._attachment_filename(path, index, total),
            )
            for index, path in enumerate(output_paths, start=1)
        ]

    async def _prepare_file_for_temporary_hosting(
        self, file: discord.File
    ) -> discord.File:
        """Normalize a default-format video before sending it to file hosting.

        A caller-supplied format must be preserved.  When the default MP4
        download happens to produce a container Discord does not render well,
        only the copy that is about to be hosted is transcoded.  Local
        attachments therefore retain their requested format and can be sent
        normally when they fit Discord's limit.
        """
        if self.format != "mp4" or not _needs_discord_mp4(file.filename):
            return file

        source_path = getattr(file.fp, "name", None)
        if not isinstance(source_path, str) or not os.path.isfile(source_path):
            return file

        original_filename = file.filename
        file.close()
        try:
            converted_path = await self._convert_to_mobile_mp4(source_path)
        except (DownloadError, OSError) as error:
            # Hosting the original is still preferable to losing the
            # download.  It will be presented as a normal link rather than an
            # attempted media-gallery embed.
            self.ctx.bot.logger.warning(
                "Could not normalize %s for temporary hosting: %s",
                original_filename,
                error,
            )
            return discord.File(source_path, filename=original_filename)

        converted_filename = os.path.splitext(original_filename)[0] + ".mp4"
        return discord.File(converted_path, filename=converted_filename)

    async def _download(self) -> discord.File:
        """Return the first item for commands that process one media file."""
        files = await self._download_all()
        for extra in files[1:]:
            extra.close()
        return files[0]

    async def _download_and_send(self) -> bool:
        started = time.time()

        try:
            files = await self._download_all()
        except DownloadError as e:
            self._cleanup_output()
            await self.ctx.send(str(e), ephemeral=self.hidden)
            return False

        elapsed = time.time() - started
        info_text = f"-# Invoked by {self.ctx.author.mention}\n-# Took {elapsed:.1f}s"

        # Discord accepts at most ten attachments per message. Keep every
        # carousel item by sending it in ordered batches when necessary.
        batches = [files[index : index + 10] for index in range(0, len(files), 10)]
        reference = self.ctx.message.to_reference(fail_if_not_exists=False)
        for batch_index, batch in enumerate(batches):
            hosted: dict[int, str] = {}
            local_files: list[discord.File] = []
            try:
                for index, file in enumerate(batch):
                    if self._file_size(file) <= self.max_filesize:
                        local_files.append(file)
                        continue
                    hosted_file = await self._prepare_file_for_temporary_hosting(file)
                    if hosted_file is not file:
                        batch[index] = hosted_file
                        files[batch_index * 10 + index] = hosted_file
                        file = hosted_file
                    hosted[index] = await upload_temporary_media(
                        self.ctx.bot,
                        file.fp,  # type: ignore[arg-type]
                        file.filename,
                        content_type=mimetypes.guess_type(file.filename)[0],
                        ignore_size_limit=self.ignore_checks,
                    )

                has_video = any(_is_gallery_file(file.filename) for file in batch)
                has_hosted = bool(hosted)
                if has_video or has_hosted:
                    container_items: list[ui.Item[Any]] = []
                    gallery_items: list[MediaGalleryItem] = []
                    other_items: list[ui.Item[Any]] = []
                    for index, file in enumerate(batch):
                        if index not in hosted and file not in local_files:
                            continue
                        if _is_gallery_file(file.filename):
                            gallery_items.append(
                                MediaGalleryItem(
                                    hosted[index]
                                    if index in hosted
                                    else f"attachment://{file.filename}"
                                )
                            )
                        elif index in hosted:
                            other_items.append(
                                ui.TextDisplay(
                                    f"[Open {file.filename}]({hosted[index]})"
                                )
                            )
                        # Non-embeddable local files remain regular Discord
                        # attachments below the component view.  Do not turn
                        # them into file components inside the embed-like
                        # container.
                    if gallery_items:
                        container_items.append(ui.MediaGallery(*gallery_items))
                    container_items.extend(other_items)
                    if has_hosted:
                        container_items.append(
                            ui.TextDisplay(
                                f"{info_text}\n"
                                "-# Oversized files are hosted for 30 minutes"
                            )
                        )
                    else:
                        container_items.append(ui.TextDisplay(info_text))
                    container = ui.Container(
                        *container_items,
                        accent_color=self.ctx.bot.embedcolor,
                    )
                    view_type = type("DownloadView", (ui.LayoutView,), {})
                    view = view_type(timeout=None)
                    view.add_item(container)
                    await self.ctx.send(
                        files=local_files,
                        view=view,
                        reference=reference if batch_index == 0 else None,
                        ephemeral=self.hidden,
                    )
                else:
                    await self.ctx.send(
                        files=local_files,
                        mention_author=True,
                        reference=reference if batch_index == 0 else None,
                        ephemeral=self.hidden,
                    )
            except TemporaryMediaError as error:
                await self.ctx.send(
                    "A file exceeded Discord's upload limit and could not be "
                    "hosted temporarily. Try a shorter or smaller media file.",
                    ephemeral=self.hidden,
                )
                self.ctx.bot.logger.warning("temporary media upload failed: %s", error)
                return False
            except discord.HTTPException:
                # A file can still be rejected for reasons other than its
                # advertised size. Retry the batch as hosted media instead of
                # falling back to an unrelated third-party file host.
                links: list[str] = [hosted[index] for index in sorted(hosted)]
                for index, file in enumerate(batch):
                    if index in hosted:
                        continue
                    hosted_file = await self._prepare_file_for_temporary_hosting(file)
                    if hosted_file is not file:
                        batch[index] = hosted_file
                        files[batch_index * 10 + index] = hosted_file
                        file = hosted_file
                    try:
                        links.append(
                            await upload_temporary_media(
                                self.ctx.bot,
                                file.fp,  # type: ignore[arg-type]
                                file.filename,
                                content_type=mimetypes.guess_type(file.filename)[0],
                                ignore_size_limit=self.ignore_checks,
                            )
                        )
                    except TemporaryMediaError as error:
                        self.ctx.bot.logger.warning(
                            "temporary media retry failed: %s", error
                        )
                if not links:
                    await self.ctx.send(
                        "Discord rejected the media and temporary hosting was "
                        "unavailable. Try a shorter or smaller file.",
                        ephemeral=self.hidden,
                    )
                    return False
                container = ui.Container(
                    ui.TextDisplay(
                        f"{info_text}\n"
                        "-# Discord rejected the upload. These links expire in 30 minutes.\n"
                        + "\n".join(f"[Open media]({link})" for link in links)
                    ),
                    accent_color=self.ctx.bot.embedcolor,
                )
                view_type = type("HostedDownloadView", (ui.LayoutView,), {})
                view = view_type(timeout=None)
                view.add_item(container)
                await self.ctx.send(
                    view=view,
                    reference=reference if batch_index == 0 else None,
                    ephemeral=self.hidden,
                )

        for file in files:
            file.close()

        for f in files:
            filename = os.path.basename(f.filename)
            if filename != f.filename:
                continue
            try:
                os.remove(DOWNLOADS_ROOT / filename)
            except OSError:
                pass

        self._cleanup_output()
        return True

    async def download(self):
        """Run the complete download/conversion/send workflow with one deadline."""
        DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)
        self._job_dir = tempfile.mkdtemp(prefix=".job-", dir=DOWNLOADS_ROOT)
        self._deadline = asyncio.get_running_loop().time() + DOWNLOAD_TIMEOUT
        try:
            async with self.ctx.bot.media_semaphore:
                completed = await asyncio.wait_for(
                    self._download_and_send(), timeout=DOWNLOAD_TIMEOUT
                )
            if completed:
                await record_download(
                    self.ctx, self.url, auto_download=self.auto_download
                )
        except asyncio.TimeoutError:
            self._cleanup_output()
            await self.ctx.send(
                f"This download took longer than {DOWNLOAD_TIMEOUT_MINUTES} minutes "
                "and was stopped. "
                "Try a shorter video.",
                ephemeral=self.hidden,
            )
        finally:
            if self._job_dir is not None:
                shutil.rmtree(self._job_dir, ignore_errors=True)
                self._job_dir = None
                self._cookie_file = None

    async def download_for_processing(
        self,
        *,
        max_bytes: int = 50 * 1024 * 1024,
        timeout: float = 60,
    ) -> tuple[bytes, str]:
        """Download media for another command without sending it to Discord."""
        DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)
        self.max_filesize = max_bytes
        self._job_dir = tempfile.mkdtemp(prefix=".effect-job-", dir=DOWNLOADS_ROOT)
        deadline = min(DOWNLOAD_TIMEOUT, max(1.0, timeout))
        self._deadline = asyncio.get_running_loop().time() + deadline
        file: discord.File | None = None
        try:
            file = await asyncio.wait_for(self._download(), timeout=deadline)
            filename = os.path.basename(file.filename)

            def read_file() -> bytes:
                file.fp.seek(0)
                return file.fp.read()

            data = await asyncio.to_thread(read_file)
            if len(data) > max_bytes:
                raise DownloadError(
                    f"This media exceeds the {max_bytes / (1024 * 1024):g} MB "
                    "effect-processing limit."
                )
            return data, filename
        except asyncio.TimeoutError as error:
            raise DownloadError(
                f"This media download took longer than {deadline:g} seconds."
            ) from error
        finally:
            if file is not None:
                file.close()
            self._cleanup_output()
            if self._job_dir is not None:
                shutil.rmtree(self._job_dir, ignore_errors=True)
                self._job_dir = None
                self._cookie_file = None

    @staticmethod
    def _file_size(file: discord.File) -> int:
        fp: Any = file.fp
        try:
            return int(os.fstat(fp.fileno()).st_size)
        except (AttributeError, OSError, ValueError):
            current = fp.tell()
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(current)
            return int(size)

    @to_thread
    def _file_to_bytes(self, file: discord.File) -> bytes:
        fp: BufferedReader | BytesIO = file.fp  # type: ignore

        if isinstance(fp, (os.PathLike, str)):
            with open(str(fp), "rb") as f:
                return f.read()
        else:
            fp.seek(0)
            return fp.read()
