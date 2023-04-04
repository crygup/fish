# main file for download utils

import secrets
from typing import Any, Dict

import yt_dlp  # type: ignore # no stub file for library

from .errors import DownloadError, InvalidWebsite, VideoIsLive
from .functions import to_thread
from .regexes import SOUNDCLOUD_RE, TIKTOK_RE, VIDEOS_RE


def match_filter(info: Dict[Any, Any]):
    if info.get("live_status", None) == "is_live":
        raise VideoIsLive()


@to_thread
def download(url: str, format: str = "mp4"):
    name = secrets.token_urlsafe(8)
    video_match = VIDEOS_RE.search(url)
    audio = False

    if video_match is None or video_match and video_match.group(0) == "":
        raise InvalidWebsite()

    video = video_match.group(0)

    options: Dict[Any, Any] = {
        "outtmpl": rf"files\downloads\{name}.%(ext)s",
        "quiet": True,
        "max_filesize": 100_000_000,
        "match_filter": match_filter,
    }

    if TIKTOK_RE.search(video):
        options["format_sort"] = ["vcodec:h264"]

    if SOUNDCLOUD_RE.search(video) or format == "mp3":
        format = "mp3"
        audio = True

    if audio:
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": format,
                "preferredquality": "192",
            }
        ]
        options["format"] = "bestaudio/best"
    else:
        options["format"] = f"bestvideo+bestaudio[ext={format}]/best"

    with yt_dlp.YoutubeDL(options) as ydl:
        try:
            ydl.download(video)  # type: ignore # yt_dlp library isn't typed
        except ValueError as e:
            raise DownloadError(str(e))

    return f"{name}.{format}"
