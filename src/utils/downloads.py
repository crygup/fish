from __future__ import annotations

import asyncio
import glob
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from io import BufferedReader, BytesIO
from typing import TYPE_CHECKING, Any, List, Optional
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import MediaGalleryItem, ui

from .errors import DownloadError
from .functions import litterbox, to_thread
from .network import validate_public_url
from .paths import DOWNLOADS_ROOT, FILES_ROOT
from .regexes import (
    INSTAGRAM_RE,
    KLIPY_RE,
    LIVE_STREAM_RE,
    SOUNDCLOUD_RE,
    TIKTOK_RE,
    TWITTER_RE,
    YOUTUBE_RE,
    YT_CLIP_RE,
    YT_SHORT_RE,
)

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
        "tiktok.com",
        "www.tiktok.com",
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
        "pin.it",
        "pinterest.com",
        "www.pinterest.com",
        "static.klipy.com",
    }
)
DISCORD_MEDIA_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi"})

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

_COOKIE_MAP: list[tuple[Any, str]] = [
    (YOUTUBE_RE, str(FILES_ROOT / "cookies" / "youtube-cookies.txt")),
    (YT_SHORT_RE, str(FILES_ROOT / "cookies" / "youtube-cookies.txt")),
    (YT_CLIP_RE, str(FILES_ROOT / "cookies" / "youtube-cookies.txt")),
    (TWITTER_RE, str(FILES_ROOT / "cookies" / "twitter-cookies.txt")),
    (INSTAGRAM_RE, str(FILES_ROOT / "cookies" / "instagram-cookies.txt")),
]


def _get_cookies(url: str) -> Optional[str]:
    for pattern, path in _COOKIE_MAP:
        if pattern.search(url) and os.path.isfile(path):
            return path
    return None


def is_discord_media_url(url: str) -> bool:
    """Return whether *url* points to a Discord-hosted attachment."""
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return hostname in DISCORD_MEDIA_HOSTS


def is_downloadable_media_page(url: str) -> bool:
    """Return whether a URL should use the guarded yt-dlp workflow."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or hostname not in DOWNLOAD_HOSTS:
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
    if SOUNDCLOUD_RE.search(url) or output_format == "mp3":
        return "bestaudio/best"
    if INSTAGRAM_RE.search(url):
        return "bestvideo+bestaudio/best"

    selector = (
        f"bestvideo[height<={res_target}]+bestaudio/" f"best[height<={res_target}]"
    )
    if TWITTER_RE.search(url) or TIKTOK_RE.search(url):
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


def _is_video_file(path: str) -> bool:
    return os.path.splitext(path)[1].casefold() in _VIDEO_SUFFIXES


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
        hidden: Optional[bool] = False,
    ) -> None:
        self.ctx = ctx
        self.url = url
        self.format = format
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

    def _find_output(self) -> str:
        matches = glob.glob(self._output_glob())
        if not matches:
            raise DownloadError("Download completed but no output file was found.")
        return matches[0]

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

    async def _yt_dlp_download(self, video: str, *, res_target: int) -> str:
        """Download via yt-dlp CLI subprocess; return the output path on disk."""

        is_youtube = bool(YOUTUBE_RE.search(video) or YT_SHORT_RE.search(video))
        is_audio = SOUNDCLOUD_RE.search(video) or self.format == "mp3"
        is_klipy = bool(KLIPY_RE.search(video))
        is_instagram = bool(INSTAGRAM_RE.search(video))
        is_tiktok = bool(TIKTOK_RE.search(video))

        format_selector = download_format_selector(
            video,
            self.format,
            res_target=res_target,
        )

        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "-f",
            format_selector,
            "-o",
            os.path.join(self._job_dir or "", f"{self.filename}.%(ext)s"),
            "--no-playlist",
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

        if cookies := _get_cookies(video):
            args += ["--cookies", self._prepare_cookie_file(cookies)]

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

        attempts = len(_TIKTOK_EXTRACTOR_PROFILES) if is_tiktok else 1
        last_returncode: int | None = None
        last_stderr = ""
        last_failure = ""

        for attempt in range(attempts):
            if attempt:
                self._cleanup_output()
                if tiktok_extractor_arg_index is not None:
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
                output_path = stdout.decode(errors="replace").strip()
                if output_path and os.path.isfile(output_path):
                    return output_path
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
            thumbnail_path = await self._instagram_thumbnail_download(video)
            if thumbnail_path is not None:
                return thumbnail_path

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
            "yt-dlp could not download this video. The site may be blocking "
            "the request, or the video may be unavailable."
        )

    async def _instagram_thumbnail_download(self, video: str) -> str | None:
        """Download a public Instagram image post through yt-dlp's thumbnail."""
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--force-ipv4",
            "--ignore-no-formats-error",
            "--skip-download",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "-o",
            os.path.join(self._job_dir or "", f"{self.filename}.%(ext)s"),
        ]

        if cookies := _get_cookies(video):
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
            raise

        if proc.returncode != 0:
            self.ctx.bot.logger.warning(
                "Instagram thumbnail fallback failed host=%s detail=%s",
                urlsplit(video).hostname,
                _summarize_yt_dlp_error(stderr_raw.decode(errors="replace")),
            )
            self._cleanup_output()
            return None

        for path in glob.glob(self._output_glob()):
            if os.path.splitext(path)[1].casefold() in _IMAGE_SUFFIXES:
                return path
        self._cleanup_output()
        return None

    async def _download_direct_media(self, url: str) -> str:
        """Download a trusted direct media URL without invoking yt-dlp."""
        if self._job_dir is None:
            raise DownloadError("The download job was not initialized.")

        suffix = os.path.splitext(urlsplit(url).path)[1].lower()
        if suffix not in {".gif", ".mp4", ".webm", ".mov"}:
            suffix = ".mp4"
        output_path = os.path.join(self._job_dir, f"{self.filename}{suffix}")

        try:
            async with asyncio.timeout(self._remaining_timeout()):
                async with self.ctx.session.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"
                        )
                    },
                ) as response:
                    if response.status != 200:
                        raise DownloadError(
                            "Klipy did not return the requested media file."
                        )

                    content_length = response.content_length
                    if (
                        content_length is not None
                        and content_length > self.max_filesize
                    ):
                        raise DownloadError(
                            f"This media exceeds {self.upload_limit_description}."
                        )

                    size = 0
                    with open(output_path, "wb") as output:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > self.max_filesize:
                                raise DownloadError(
                                    f"This media exceeds {self.upload_limit_description}."
                                )
                            output.write(chunk)
        except DownloadError:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise DownloadError(
                "Klipy did not return the requested media file."
            ) from exc

        return output_path

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
        """Transcode Instagram media to a broadly supported MP4 format."""
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
            self._cleanup_output()
            raise

        if proc.returncode != 0:
            self._cleanup_output()
            stderr = stderr_raw.decode(errors="replace").strip() if stderr_raw else ""
            tail = stderr.rsplit("\n", 1)[-1] if stderr else "unknown error"
            raise DownloadError(f"Video compatibility conversion failed: {tail}")

        try:
            os.replace(output_path, final_path)
            if input_path != final_path:
                os.remove(input_path)
        except OSError as exc:
            self._cleanup_output()
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

    async def _download(self) -> discord.File:
        await validate_public_url(self.url, allowed_hosts=DOWNLOAD_HOSTS)

        if any(pattern.search(self.url) for pattern in LIVE_STREAM_RE):
            raise DownloadError(
                "Live streams cannot be downloaded. Please provide a recorded video."
            )

        is_audio = SOUNDCLOUD_RE.search(self.url) or self.format == "mp3"
        # Klipy's resolver returns a direct static media URL.  Keep track of
        # that host because the original klipy.com page URL is no longer
        # available here after resolution.
        is_klipy_media = (
            urlsplit(self.url).scheme == "https"
            and urlsplit(self.url).hostname == "static.klipy.com"
        )

        if is_klipy_media:
            output_path = await self._download_direct_media(self.url)
        else:
            output_path = await self._yt_dlp_download(self.url, res_target=1080)

        if is_audio:
            if os.path.getsize(output_path) > self.max_filesize:
                self._cleanup_output()
                raise DownloadError(
                    f"This audio file exceeds {self.upload_limit_description}."
                )
            return discord.File(output_path, filename=os.path.basename(output_path))

        if os.path.getsize(output_path) > self.max_filesize:
            self._cleanup_output()
            if is_klipy_media:
                raise DownloadError(
                    f"This media exceeds {self.upload_limit_description}."
                )
            output_path = await self._yt_dlp_download(self.url, res_target=720)

            if os.path.getsize(output_path) > self.max_filesize:
                self._cleanup_output()
                raise DownloadError(
                    f"This video exceeds {self.upload_limit_description} even at "
                    "720p. Try a shorter clip."
                )

        # Instagram may return VP9 video in an MP4 container. Desktop players
        # can decode it, but Discord mobile can display only the first frame.
        # Normalize Instagram downloads to H.264/AAC with fast-start metadata.
        if INSTAGRAM_RE.search(self.url) and _is_video_file(output_path):
            output_path = await self._convert_to_mobile_mp4(output_path)
            if os.path.getsize(output_path) > self.max_filesize:
                self._cleanup_output()
                raise DownloadError(
                    f"This video exceeds {self.upload_limit_description} after "
                    "mobile compatibility conversion. Try a shorter clip."
                )

        # Twitter GIF conversion:
        #   --format gif          → always convert (rejected if > 30s)
        #   --format mp4 (default) → auto-convert if file has no audio track
        if TWITTER_RE.search(self.url):
            want_gif = self.format == "gif" or (
                self.format == "mp4" and not await self._has_audio(output_path)
            )
            if want_gif:
                if self.format == "gif" and self._duration > 30:
                    raise DownloadError(
                        "GIF conversion is limited to videos 30 seconds or shorter. "
                        "This video is {:.0f} seconds.".format(self._duration)
                    )
                dur = self._duration or await self._get_duration(output_path)
                if dur > 30:
                    raise DownloadError(
                        "GIF conversion is limited to videos 30 seconds or shorter. "
                        "This video is {:.0f} seconds.".format(dur)
                    )
                output_path = await self._convert_to_gif(output_path)
                self.format = "gif"

                if os.path.getsize(output_path) > self.max_filesize:
                    self._cleanup_output()
                    raise DownloadError(
                        f"GIF conversion exceeded {self.upload_limit_description}. "
                        "Try a shorter clip or omit `--gif`."
                    )

        # Klipy pages represent GIFs as video files.  Normalize those files to
        # an actual GIF before sending so Discord renders them as animated GIFs
        # instead of a video attachment.
        if is_klipy_media and not is_audio and not output_path.lower().endswith(".gif"):
            dur = self._duration or await self._get_duration(output_path)
            if dur > 30:
                self._cleanup_output()
                raise DownloadError(
                    "GIF conversion is limited to videos 30 seconds or shorter. "
                    "This video is {:.0f} seconds.".format(dur)
                )
            output_path = await self._convert_to_gif(output_path, full_frames=True)
            self.format = "gif"

            if os.path.getsize(output_path) > self.max_filesize:
                self._cleanup_output()
                raise DownloadError(
                    f"GIF conversion exceeded {self.upload_limit_description}. "
                    "Try a shorter clip."
                )

        return discord.File(output_path, filename=os.path.basename(output_path))

    async def _download_and_send(self):
        files: List[discord.File] = []

        started = time.time()

        try:
            files.append(await self._download())
        except DownloadError as e:
            self._cleanup_output()
            await self.ctx.send(str(e), ephemeral=self.hidden)
            return

        elapsed = time.time() - started
        info_text = f"-# Invoked by {self.ctx.author.mention}\n-# Took {elapsed:.1f}s"

        file = files[0]
        filename = file.filename.lower()

        try:
            if filename.endswith((".mp4", ".webm", ".mov")):
                gallery = ui.MediaGallery(
                    MediaGalleryItem(f"attachment://{file.filename}")
                )
                container = ui.Container(
                    gallery,
                    ui.TextDisplay(info_text),
                    accent_color=self.ctx.bot.embedcolor,
                )
                view_type = type("DownloadView", (ui.LayoutView,), {})
                v = view_type(timeout=None)
                v.add_item(container)
                await self.ctx.send(
                    file=file,
                    view=v,
                    reference=self.ctx.message.to_reference(fail_if_not_exists=False),
                    ephemeral=self.hidden,
                )
            else:
                await self.ctx.send(
                    files=files,
                    mention_author=True,
                    reference=self.ctx.message.to_reference(fail_if_not_exists=False),
                    ephemeral=self.hidden,
                )
        except discord.HTTPException:
            text = (
                "Files were too big for Discord. "
                "**These will delete after 72 hours**\n\n"
            )
            for f in files:
                file_bytes = await self._file_to_bytes(f)
                url = await litterbox(self.ctx.session, file_bytes, f.filename)
                text += f"{url}\n"
            await self.ctx.send(text, ephemeral=self.hidden)

        for f in files:
            filename = os.path.basename(f.filename)
            if filename != f.filename:
                continue
            try:
                os.remove(DOWNLOADS_ROOT / filename)
            except OSError:
                pass

        self._cleanup_output()

    async def download(self):
        """Run the complete download/conversion/send workflow with one deadline."""
        DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)
        self._job_dir = tempfile.mkdtemp(prefix=".job-", dir=DOWNLOADS_ROOT)
        self._deadline = asyncio.get_running_loop().time() + DOWNLOAD_TIMEOUT
        try:
            async with self.ctx.bot.media_semaphore:
                await asyncio.wait_for(
                    self._download_and_send(), timeout=DOWNLOAD_TIMEOUT
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

    @to_thread
    def _file_to_bytes(self, file: discord.File) -> bytes:
        fp: BufferedReader | BytesIO = file.fp  # type: ignore

        if isinstance(fp, (os.PathLike, str)):
            with open(str(fp), "rb") as f:
                return f.read()
        else:
            return fp.read()
