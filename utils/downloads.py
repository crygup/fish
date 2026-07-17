from __future__ import annotations

import glob
import os
import re
import secrets
import signal
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlsplit

import asyncio
import discord
from discord import ui, MediaGalleryItem
import sys

from .errors import DownloadError, InvalidWebsite
from .functions import to_thread, litterbox
from .regexes import (
    INSTAGRAM_RE,
    SOUNDCLOUD_RE,
    TIKTOK_RE,
    TWITCH_RE,
    TWITTER_RE,
    YOUTUBE_RE,
    YT_CLIP_RE,
    YT_SHORT_RE,
    KLIPY_RE,
    LIVE_STREAM_RE,
)
from io import BufferedReader, BytesIO

if TYPE_CHECKING:
    from core import Context

MAX_FILESIZE: int = 50_000_000  # 50 MB
DOWNLOAD_TIMEOUT: float = 180.0  # three minutes

_COOKIE_MAP: list[tuple[Any, str]] = [
    (YOUTUBE_RE, "files/cookies/youtube-cookies.txt"),
    (YT_SHORT_RE, "files/cookies/youtube-cookies.txt"),
    (YT_CLIP_RE, "files/cookies/youtube-cookies.txt"),
    (TWITTER_RE, "files/cookies/twitter-cookies.txt"),
    (INSTAGRAM_RE, "files/cookies/instagram-cookies.txt"),
]


def _get_cookies(url: str) -> Optional[str]:
    for pattern, path in _COOKIE_MAP:
        if pattern.search(url) and os.path.isfile(path):
            return path
    return None


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
        self._duration = 0
        self._deadline: float | None = None

    def _remaining_timeout(self) -> float:
        if self._deadline is None:
            return DOWNLOAD_TIMEOUT
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise DownloadError(
                "This download took longer than 3 minutes and was stopped. "
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
                "This download took longer than 3 minutes and was stopped. "
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
        return f"files/downloads/{self.filename}.*"

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

    async def _yt_dlp_download(self, video: str, *, res_target: int) -> str:
        """Download via yt-dlp CLI subprocess; return the output path on disk."""

        is_youtube = bool(YOUTUBE_RE.search(video) or YT_SHORT_RE.search(video))
        is_audio = SOUNDCLOUD_RE.search(video) or self.format == "mp3"
        is_twitter = bool(TWITTER_RE.search(video))
        is_klipy = bool(KLIPY_RE.search(video))

        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "-f",
            "bestaudio/best" if is_audio else "best",
            "-o",
            f"files/downloads/{self.filename}.%(ext)s",
            "--no-playlist",
            "--js-runtimes",
            "deno",
            "--print",
            "after_move:filepath",
        ]

        if cookies := _get_cookies(video):
            args += ["--cookies", cookies]

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

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
            + ":/home/zil/.deno/bin",
            "HOME": os.environ.get("HOME", "/home/zil"),
        }
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr_raw = await self._communicate_with_timeout(proc)
        except DownloadError:
            self._cleanup_output()
            raise
        stderr_text = stderr_raw.decode().strip() if stderr_raw else ""

        if proc.returncode != 0:
            self.ctx.bot.logger.error(
                f"yt-dlp failed (exit {proc.returncode}). Command: {' '.join(args)}"
            )
            if stderr_text:
                self.ctx.bot.logger.error(f"yt-dlp stderr:\n{stderr_text}")
            if "is_live" in stderr_text and "filter" in stderr_text:
                raise DownloadError(
                    "Live streams cannot be downloaded. Please provide a recorded video."
                )
            err = stderr_text or "unknown error"
            # Extract the last meaningful line for the user.
            for line in reversed(err.splitlines()):
                line = line.strip()
                if line and not line.startswith("[") and "WARNING" not in line:
                    err = line
                    break
            raise DownloadError(f"yt-dlp was unable to download this video: {err}")
        output_path = stdout.decode().strip()
        if not output_path or not os.path.isfile(output_path):
            raise DownloadError(
                "yt-dlp was unable to download this video. "
                "The site may be blocking the request, or the video is unavailable."
            )

        self.ctx.bot.current_downloads.append(os.path.basename(output_path))
        return output_path

    async def _convert_to_gif(self, input_path: str) -> str:
        """Convert a video file to an optimized GIF via ffmpeg.

        Returns the path to the new ``.gif`` file (the original is removed).
        """
        output_path = input_path.rsplit(".", 1)[0] + ".gif"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i",
            input_path,
            "-vf",
            GIF_FILTER,
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
        _ALLOWED = (
            YOUTUBE_RE,
            YT_SHORT_RE,
            YT_CLIP_RE,
            INSTAGRAM_RE,
            TIKTOK_RE,
            SOUNDCLOUD_RE,
            TWITTER_RE,
            TWITCH_RE,
        )

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

        output_path = await self._yt_dlp_download(self.url, res_target=1080)

        if is_audio:
            return discord.File(output_path, filename=os.path.basename(output_path))

        if os.path.getsize(output_path) > MAX_FILESIZE:
            self._cleanup_output()
            output_path = await self._yt_dlp_download(self.url, res_target=720)

            if os.path.getsize(output_path) > MAX_FILESIZE:
                self._cleanup_output()
                raise DownloadError(
                    "This video exceeds 50 MB even at 720p. Try a shorter clip."
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

                if os.path.getsize(output_path) > MAX_FILESIZE:
                    self._cleanup_output()
                    raise DownloadError(
                        "GIF conversion exceeded 50 MB. Try a shorter clip or omit `--gif`."
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
            output_path = await self._convert_to_gif(output_path)
            self.format = "gif"

            if os.path.getsize(output_path) > MAX_FILESIZE:
                self._cleanup_output()
                raise DownloadError(
                    "GIF conversion exceeded 50 MB. Try a shorter clip."
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
                os.remove(os.path.join("files/downloads", filename))
            except OSError:
                pass

        self._cleanup_output()

    async def download(self):
        """Run the complete download/conversion/send workflow with one deadline."""
        self._deadline = asyncio.get_running_loop().time() + DOWNLOAD_TIMEOUT
        try:
            await asyncio.wait_for(self._download_and_send(), timeout=DOWNLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            self._cleanup_output()
            await self.ctx.send(
                "This download took longer than 3 minutes and was stopped. "
                "Try a shorter video.",
                ephemeral=self.hidden,
            )

    @to_thread
    def _file_to_bytes(self, file: discord.File) -> bytes:
        fp: BufferedReader | BytesIO = file.fp  # type: ignore

        if isinstance(fp, (os.PathLike, str)):
            with open(str(fp), "rb") as f:
                return f.read()
        else:
            return fp.read()
