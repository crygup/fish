from __future__ import annotations

import asyncio
import datetime
import re
import time
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    ClassVar,
    Optional,
    ParamSpec,
    Sequence,
    TypeVar,
    Union,
)

import discord
from dateutil.relativedelta import relativedelta
from discord.ext import commands
from PIL import Image, ImageSequence

if TYPE_CHECKING:
    from bot import Bot

__all__ = [
    "cleanup_code",
    "to_thread",
    "plural",
    "Timer",
    "human_timedelta",
    "resize_to_limit",
    "Regexes",
    "GuildContext",
    "run",
    "regexes",
    "get_video",
]

T = TypeVar("T")
P = ParamSpec("P")


regexes = {
    "VMtiktok": {
        "regex": r"https?:\/\/vm.tiktok.com\/[a-zA-Z0-9_-]{9}",
        "nsfw": False,
        "whitelist": False,
    },
    "WEBtiktok": {
        "regex": r"https?:\/\/(www.)?tiktok.com\/@?[a-zA-Z0-9_]{4,24}\/video\/[0-9]{19}",
        "nsfw": False,
        "whitelist": False,
    },
    "instagram": {
        "regex": r"https:\/\/(www.)?instagram.com\/(p|tv|reel)\/[a-zA-Z0-9]{11}\/",
        "nsfw": False,
        "whitelist": False,
    },
    "twitch": {
        "regex": r"https?:\/\/clips.twitch.tv\/[a-zA-Z0-9_-]*",
        "nsfw": False,
        "whitelist": True,
    },
    "twitter": {
        "regex": r"https?:\/\/twitter.com\/[a-zA-Z0-9_]{2,15}\/status\/[0-9]{19}",
        "nsfw": True,
        "whitelist": False,
    },
    "reddit": {
        "regex": r"https?:\/\/(www.)reddit.com\/r\/[a-zA-Z0-9_-]{1,20}\/comments\/[a-z0-9]{6}",
        "nsfw": True,
        "whitelist": False,
    },
    "youtube_short": {
        "regex": r"https:\/\/(www.)?youtube.com\/shorts\/[a-zA-Z0-9_-]{11}",
        "nsfw": False,
        "whitelist": True,
    },
    "youtube": {
        "regex": r"https:\/\/(www.)?youtu(.be|be.com)\/(watch\?v=[a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]{11})",
        "nsfw": False,
        "whitelist": True,
    },
    "pornhub": {
        "regex": r"(https://)?(www.)?pornhub.com/view_video.php\?viewkey=[a-zA-Z0-9]{0,20}",
        "nsfw": True,
        "whitelist": True,
    }
}


async def get_video(ctx: GuildContext, url: str) -> Optional[str]:
    for regex in regexes:
        result = re.search(regexes[regex]["regex"], url)
        if result:
            if regexes[regex]["whitelist"]:
                if ctx.author.id not in ctx.bot.whitelisted_users:
                    raise commands.BadArgument(
                        "You are not whitelisted to use this service, contact cr#0333."
                    )
            if regexes[regex]["nsfw"]:
                if not ctx.channel.is_nsfw():
                    raise commands.BadArgument(
                        "The site given has been marked as NSFW, please switch to a NSFW channel."
                    )

            return result.group(0)

    return None


async def run(cmd: str) -> Optional[str]:

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await proc.communicate()

    if stdout:
        return f"[stdout]\n{stdout.decode()}"

    if stderr:
        raise TypeError(f"[stderr]\n{stderr.decode()}")


class GuildContext(commands.Context):
    bot: Bot
    author: discord.Member
    guild: discord.Guild
    channel: Union[discord.VoiceChannel, discord.TextChannel, discord.Thread]
    me: discord.Member
    prefix: str


class Regexes:
    TENOR_PAGE_REGEX: ClassVar[re.Pattern] = re.compile(
        r"https?://(www\.)?tenor\.com/view/\S+"
    )
    TENOR_GIF_REGEX: ClassVar[re.Pattern] = re.compile(
        r"https?://(www\.)?c\.tenor\.com/\S+/\S+\.gif"
    )
    CUSTOM_EMOJI_REGEX: ClassVar[re.Pattern] = re.compile(
        r"<(a)?:([a-zA-Z0-9_]{2,32}):([0-9]{18,22})>"
    )


def cleanup_code(content: str) -> str:
    """Automatically removes code blocks from the code."""

    if content.startswith("```") and content.endswith("```"):
        return "\n".join(content.split("\n")[1:-1])

    return content.strip("` \n")


class plural:
    def __init__(self, value: int):
        self.value: int = value

    def __format__(self, format_spec: str) -> str:
        v = self.value
        singular, sep, plural = format_spec.partition("|")
        plural = plural or f"{singular}s"
        if abs(v) != 1:
            return f"{v} {plural}"
        return f"{v} {singular}"


def to_thread(func: Callable[P, T]) -> Callable[P, Awaitable[T]]:
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


class Timer:
    def __init__(self):
        self._start = None
        self._end = None

    def start(self):
        self._start = time.perf_counter()

    def stop(self):
        self._end = time.perf_counter()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def __int__(self):
        return round(self.time)

    def __float__(self):
        return self.time

    def __str__(self):
        return str(self.time)

    def __repr__(self):
        return f"<Timer time={self.time}>"

    @property
    def time(self):
        if self._end is None or self._start is None:
            return 0

        return self._end - self._start


def human_join(seq: Sequence[str], delim: str = ", ", final: str = "or") -> str:
    size = len(seq)
    if size == 0:
        return ""

    if size == 1:
        return seq[0]

    if size == 2:
        return f"{seq[0]} {final} {seq[1]}"

    return delim.join(seq[:-1]) + f" {final} {seq[-1]}"


def human_timedelta(
    dt: datetime.datetime,
    *,
    source: Optional[datetime.datetime] = None,
    accuracy: Optional[int] = 3,
    brief: bool = False,
    suffix: bool = True,
) -> str:
    now = source or datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    # Microsecond free zone
    now = now.replace(microsecond=0)
    dt = dt.replace(microsecond=0)

    # This implementation uses relativedelta instead of the much more obvious
    # divmod approach with seconds because the seconds approach is not entirely
    # accurate once you go over 1 week in terms of accuracy since you have to
    # hardcode a month as 30 or 31 days.
    # A query like "11 months" can be interpreted as "!1 months and 6 days"
    if dt > now:
        delta = relativedelta(dt, now)
        output_suffix = ""
    else:
        delta = relativedelta(now, dt)
        output_suffix = " ago" if suffix else ""

    attrs = [
        ("year", "y"),
        ("month", "mo"),
        ("day", "d"),
        ("hour", "h"),
        ("minute", "m"),
        ("second", "s"),
    ]

    output = []
    for attr, brief_attr in attrs:
        elem = getattr(delta, attr + "s")
        if not elem:
            continue

        if attr == "day":
            weeks = delta.weeks
            if weeks:
                elem -= weeks * 7
                if not brief:
                    output.append(format(plural(weeks), "week"))
                else:
                    output.append(f"{weeks}w")

        if elem <= 0:
            continue

        if brief:
            output.append(f"{elem}{brief_attr}")
        else:
            output.append(format(plural(elem), attr))

    if accuracy is not None:
        output = output[:accuracy]

    if len(output) == 0:
        return "now"
    else:
        if not brief:
            return human_join(output, final="and") + output_suffix
        else:
            return " ".join(output) + output_suffix


def format_relative(dt: datetime.datetime) -> str:
    return discord.utils.format_dt(dt, "R")


# https://github.com/CuteFwan/Koishi/blob/master/cogs/utils/images.py#L4-L34
def resize_to_limit(data: BytesIO, limit: int) -> BytesIO:
    """
    Downsize it for huge PIL images.
    Half the resolution until the byte count is within the limit.
    """
    current_size = data.getbuffer().nbytes
    while current_size > limit:
        with Image.open(data) as im:
            data = BytesIO()
            if im.format == "PNG":
                im = im.resize(tuple([i // 2 for i in im.size]), resample=Image.BICUBIC)
                im.save(data, "png")
            elif im.format == "GIF":
                durations = []
                new_frames = []
                for frame in ImageSequence.Iterator(im):
                    durations.append(frame.info["duration"])
                    new_frames.append(
                        frame.resize([i // 2 for i in im.size], resample=Image.BICUBIC)
                    )
                new_frames[0].save(
                    data,
                    save_all=True,
                    append_images=new_frames[1:],
                    format="gif",
                    version=im.info["version"],
                    duration=durations,
                    loop=0,
                    transparency=0,
                    background=im.info["background"],
                    palette=im.getpalette(),
                )
            data.seek(0)
            current_size = data.getbuffer().nbytes
    return data
