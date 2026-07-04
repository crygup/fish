from __future__ import annotations

import glob
import os
import secrets
import subprocess
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import asyncio
import discord
import sys

from .errors import DownloadError, InvalidWebsite
from .functions import to_thread, run, litterbox
from .regexes import (
    INSTAGRAM_RE,
    SOUNDCLOUD_RE,
    TIKTOK_RE,
    TWITTER_RE,
    YOUTUBE_RE,
    YT_SHORT_RE,
)
from io import BufferedReader, BytesIO

if TYPE_CHECKING:
    from core import Context

MAX_FILESIZE: int = 50_000_000  # 50 MB

_COOKIE_MAP: list[tuple[Any, str]] = [
    (YOUTUBE_RE, "files/cookies/youtube-cookies.txt"),
    (YT_SHORT_RE, "files/cookies/youtube-cookies.txt"),
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
        self.filename = filename or secrets.token_urlsafe(8).strip("-")
        self.hidden = hidden
        self._duration = 0

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

        args = [
            sys.executable, "-m", "yt_dlp",
            "-f", "bestaudio/best" if is_audio else "best",
            "-o", f"files/downloads/{self.filename}.%(ext)s",
            "--no-playlist",
            "--js-runtimes", "deno",
            "--print", "after_move:filepath",
        ]

        if cookies := _get_cookies(video):
            args += ["--cookies", cookies]

        if not is_youtube:
            args += [
                "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "--add-header", "Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "--add-header", "Accept-Language:en-US,en;q=0.9",
            ]

        if is_audio:
            args += ["--extract-audio", "--audio-format", "mp3"]
            self.format = "mp3"

        args.append(video)

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin") + ":/home/zil/.deno/bin",
            "HOME": os.environ.get("HOME", "/home/zil"),
        }
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr_raw = await proc.communicate()
        stderr_text = stderr_raw.decode().strip() if stderr_raw else ""

        if proc.returncode != 0:
            self.ctx.bot.logger.error(
                f"yt-dlp failed (exit {proc.returncode}). Command: {' '.join(args)}"
            )
            if stderr_text:
                self.ctx.bot.logger.error(f"yt-dlp stderr:\n{stderr_text}")
            err = stderr_text or "unknown error"
            # Extract the last meaningful line for the user.
            for line in reversed(err.splitlines()):
                line = line.strip()
                if line and not line.startswith("[") and "WARNING" not in line:
                    err = line
                    break
            raise DownloadError(
                f"yt-dlp was unable to download this video: {err}"
            )
        output_path = stdout.decode().strip()
        if not output_path or not os.path.isfile(output_path):
            raise DownloadError(
                "yt-dlp was unable to download this video. "
                "The site may be blocking the request, or the video is unavailable."
            )

        self.ctx.bot.current_downloads.append(os.path.basename(output_path))
        return output_path

    @to_thread
    def _convert_to_gif(self, input_path: str) -> str:
        """Convert a video file to an optimized GIF via ffmpeg.

        Returns the path to the new ``.gif`` file (the original is removed).
        """
        output_path = input_path.rsplit(".", 1)[0] + ".gif"
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                input_path,
                "-vf",
                GIF_FILTER,
                "-y",
                output_path,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            os.remove(input_path)
            tail = (
                result.stderr.strip().rsplit("\n", 1)[-1]
                if result.stderr
                else "unknown error"
            )
            raise DownloadError(f"GIF conversion failed: {tail}")

        os.remove(input_path)
        return output_path

    async def _has_audio(self, path: str) -> bool:
        """Return True if *path* contains an audio stream (ffprobe)."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode == 0 and b"audio" in stdout

    async def _get_duration(self, path: str) -> float:
        """Return duration in seconds from ffprobe, or 0 on failure."""
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
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
            INSTAGRAM_RE,
            TIKTOK_RE,
            SOUNDCLOUD_RE,
            TWITTER_RE,
        )
        if not any(p.search(self.url) for p in _ALLOWED):
            raise InvalidWebsite()

        is_audio = SOUNDCLOUD_RE.search(self.url) or self.format == "mp3"

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

        return discord.File(output_path, filename=os.path.basename(output_path))

    async def download(self):
        files: List[discord.File] = []

        try:
            files.append(await self._download())
        except DownloadError as e:
            await self.ctx.send(str(e), ephemeral=self.hidden)
            return

        try:
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
            for file in files:
                file_bytes = await self._file_to_bytes(file)
                url = await litterbox(self.ctx.session, file_bytes, file.filename)
                text += f"{url}\n"
            await self.ctx.send(text, ephemeral=self.hidden)

        for file in files:
            try:
                await run(f'cd files/downloads && rm "{file.filename}"')
            except Exception:
                pass

        self._cleanup_output()

    @to_thread
    def _file_to_bytes(self, file: discord.File) -> bytes:
        fp: BufferedReader | BytesIO = file.fp  # type: ignore

        if isinstance(fp, (os.PathLike, str)):
            with open(str(fp), "rb") as f:
                return f.read()
        else:
            return fp.read()