from __future__ import annotations

import glob
import os
import secrets
import subprocess
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import discord
import yt_dlp

from .errors import DownloadError, InvalidWebsite, VideoIsLive
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


class _YtDlpLogger:
    """Routes yt-dlp log output to the bot's logger so failures are visible."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def debug(self, msg: str) -> None:
        self._logger.debug(msg)

    def info(self, msg: str) -> None:
        self._logger.info(msg)

    def warning(self, msg: str) -> None:
        self._logger.warning(msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)


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


def match_filter(info: Dict[Any, Any]):
    if info.get("live_status", None) == "is_live":
        raise VideoIsLive()


class Downloader:
    def __init__(
        self,
        ctx: Context,
        url: str,
        format: str = "mp4",
        filename: Optional[str] = None,
        hidden: Optional[bool] = False,
        twitter_gif: bool = False,
    ) -> None:
        self.ctx = ctx
        self.url = url
        self.format = format
        self.filename = filename or secrets.token_urlsafe(8).strip("-")
        self.hidden = hidden
        self.twitter_gif = twitter_gif

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


    @to_thread
    def _yt_dlp_download(self, video: str, *, res_target: int) -> str:
        """Download via yt-dlp; return the output *path* on disk.

        ``res_target`` is the smaller-dimension target (1080 or 720);
        yt-dlp's ``res`` sort key handles both landscape and portrait.
        """
        audio = SOUNDCLOUD_RE.search(video) or self.format == "mp3"

        options: Dict[Any, Any] = {
            "outtmpl": rf"files/downloads/{self.filename}.%(ext)s",
            "quiet": False,
            "match_filter": match_filter,
            "logger": _YtDlpLogger(self.ctx.bot.logger),
        }

        if cookies := _get_cookies(video):
            options["cookiefile"] = cookies

        is_youtube = bool(YOUTUBE_RE.search(video) or YT_SHORT_RE.search(video))

        if is_youtube:
            # Let yt-dlp fetch the EJS challenge solver from GitHub
            # (first run downloads + caches; subsequent runs are instant).
            options["remote_components"] = ["ejs:github"]
            # Try deno/node for JS challenges; if deno isn't in the bot's
            # PATH, set DENO_PATH in the environment.
            _deno = os.environ.get("DENO_PATH", "deno")
            options["js_runtimes"] = {"deno": {"path": _deno}, "node": {}}

        rc = 1

        if audio:
            self.format = "mp3"
            options["format"] = "bestaudio/best"
            options.setdefault("postprocessors", []).append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            )

            with yt_dlp.YoutubeDL(options) as ydl:  # type: ignore
                try:
                    ydl.download([video])
                except Exception as e:
                    raise DownloadError(str(e)) from e

            rc = 0
        else:
            if is_youtube:
                has_cookies = bool(options.get("cookiefile"))
                attempts = []

                if has_cookies:
                    attempts.append(("best", ["web"], True))

                attempts += [
                    ("best", ["android"], False),
                    ("best", None, False),
                ]
            else:
                attempts = [("bestvideo+bestaudio/best", None, False)]

            rc = 1
            for fmt, clients, use_cookies in attempts:
                opts = dict(options)
                opts["format"] = fmt
                if clients is not None:
                    opts.setdefault("extractor_args", {}).setdefault("youtube", {})[
                        "player_client"
                    ] = clients
                else:
                    opts.pop("extractor_args", None)
                if not use_cookies:
                    opts.pop("cookiefile", None)

                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                    try:
                        rc = ydl.download([video])
                    except Exception:
                        rc = 1

                if rc == 0:
                    break

        if rc != 0:
            raise DownloadError(
                "yt-dlp was unable to download this video. "
                "The site may be blocking the request, or the video is unavailable."
            )

        output_path = self._find_output()
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

        if self.twitter_gif and TWITTER_RE.search(self.url):
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