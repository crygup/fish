from typing import Any

from discord.ext import commands


class DownloadError(commands.CommandError):
    def __init__(self, message: str, *args: Any) -> None:
        self.message: str = message
        super().__init__(message, *args)


class VideoIsLive(DownloadError):
    def __init__(
        self, message: str = "You are not allowed to download live videos.", *args: Any
    ) -> None:
        self.message: str = message
        super().__init__(message, *args)


class InvalidWebsite(DownloadError):
    def __init__(
        self,
        message: str = "Unaccepted website. Only TikTok, Twitch, Instagram, YouTube, Reddit and Soundcloud are accepted right now.",
        *args: Any
    ) -> None:
        self.message: str = message
        super().__init__(message, *args)

ignored_errors = (commands.NotOwner)
valid_errors = (commands.BadArgument, InvalidWebsite, VideoIsLive, DownloadError, commands.CommandError)