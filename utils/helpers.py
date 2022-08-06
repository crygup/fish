from __future__ import annotations

import asyncio
import datetime
import math
import re
import sys
import textwrap
import time
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    ClassVar,
    List,
    Optional,
    ParamSpec,
    Sequence,
    TypeVar,
    Union,
)

import discord
from aiohttp import ClientResponse
from cogs.context import Context
from dateutil.relativedelta import relativedelta
from discord.ext import commands
from PIL import Image, ImageSequence

from .errors import *

if TYPE_CHECKING:
    from bot import Bot

T = TypeVar("T")
P = ParamSpec("P")

lastfm_period = {
    "overall": "overall",
    "7day": "weekly",
    "1month": "monthly",
    "3month": "quarterly",
    "6month": "half-yearly",
    "12month": "yearly",
}

emoji_regex = r"<(?P<animated>a)?:(?P<name>[a-zA-Z0-9\_]{1,}):(?P<id>[0-9]{1,})>"


class AuthorView(discord.ui.View):
    def __init__(self, ctx: Context, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)

        self.message: Optional[discord.Message] = None
        self.ctx = ctx

    def disable_all(self) -> None:
        for button in self.children:
            if isinstance(button, discord.ui.Button):
                button.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message:
            await self.message.edit(view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user and interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message(
            f'You can\'t use this, sorry. \nIf you\'d like to use this then run the command `{self.ctx.command}{self.ctx.invoked_subcommand or ""}`',
            ephemeral=True,
        )
        return False


async def mobile(self) -> None:
    """Sends the IDENTIFY packet."""
    payload = {
        "op": self.IDENTIFY,
        "d": {
            "token": self.token,
            "properties": {
                "$os": sys.platform,
                "$browser": "Discord iOS",
                "$device": "discord.py",
                "$referrer": "",
                "$referring_domain": "",
            },
            "compress": True,
            "large_threshold": 250,
        },
    }

    if self.shard_id is not None and self.shard_count is not None:
        payload["d"]["shard"] = [self.shard_id, self.shard_count]

    state = self._connection
    if state._activity is not None or state._status is not None:
        payload["d"]["presence"] = {
            "status": state._status,
            "game": state._activity,
            "since": 0,
            "afk": False,
        }

    if state._intents is not None:
        payload["d"]["intents"] = state._intents.value

    await self.call_hooks(
        "before_identify", self.shard_id, initial=self._initial_identify
    )
    await self.send_as_json(payload)


def add_prefix(bot: Bot, guild_id: int, prefix: str):
    try:
        bot.prefixes[guild_id].append(prefix)
    except KeyError:
        bot.prefixes[guild_id] = [prefix]


async def get_lastfm(bot: Bot, user_id: int) -> str:
    """Get the last.fm username for the given user ID."""
    name = await bot.redis.hget(f"accounts:{user_id}", "lastfm")
    if not name:
        raise UnknownAccount("No last.fm account set.")
    return name


async def get_roblox(bot: Bot, user_id: int) -> str:
    """Get the roblox username for the given user ID."""
    name = await bot.redis.hget(f"accounts:{user_id}", "roblox")
    if not name:
        raise UnknownAccount("No roblox account set.")
    return name


async def get_genshin(bot: Bot, user_id: int) -> str:
    """Get the genshin UID for the given user ID."""
    name = await bot.redis.hget(f"accounts:{user_id}", "genshin")
    if not name:
        raise UnknownAccount("No genshin account set.")
    return name


async def get_osu(bot: Bot, user_id: int) -> str:
    """Get the osu! username for the given user ID."""
    name = await bot.redis.hget(f"accounts:{user_id}", "osu")
    if name is None:
        raise UnknownAccount("No osu! account set.")
    return name


USER_FLAGS = {
    "staff": "<:staff:949147468124262420> Discord Staff",
    "partner": "<:partner:949147457839829043> Discord Partner",
    "hypesquad": "<:hypesquad:949147451942649916> HypeSquad",
    "bug_hunter": "<:bughunterlv1:949147440219553873> Bug Hunter",
    "bug_hunter_level_2": "<:bughunterlv2:949147441935024178> Bug Hunter 2",
    "hypesquad_bravery": "<:bravery:949147435333218305> HypeSquad Bravery",
    "hypesquad_brilliance": "<:brillance:949147436880912405> HypeSquad Brilliance",
    "hypesquad_balance": "<:balance:949147429733793832> HypeSquad Balance",
    "early_supporter": "<:earlysupporter:949147447756726342> Early Supporter",
    "verified_bot_developer": "<:bot_dev:949147434204946472> Bot Developer",
    "verified_bot": "<:bot:949147432598515723> Verified Bot",
    "discord_certified_moderator": "<:certified_moderator:949147443264622643> Moderator",
    "system": "<:system:949147469357387817> System",
}


async def get_user_badges(
    member: Union[discord.Member, discord.User],
    ctx: Context,
    fetched_user: Optional[discord.User] = None,
) -> List:
    flags = dict(member.public_flags)

    user_flags = []
    if await ctx.bot.is_owner(member):
        user_flags.append(f"<:cr_owner:972016928371654676> Bot Owner")

    if isinstance(member, discord.Member) and member.guild.owner == member:
        user_flags.append(f"<:owner:949147456376033340> Server Owner")

    if isinstance(member, discord.Member) and member.premium_since:
        user_flags.append(f"<:booster:949147430786596896> Server Booster")

    if member.display_avatar.is_animated() or fetched_user and fetched_user.banner:
        user_flags.append(f"<:nitro:949147454991896616> Nitro")

    for flag, text in USER_FLAGS.items():
        try:
            if flags[flag]:
                user_flags.append(text)
        except KeyError:
            continue

    return user_flags


video_regexes = {
    "VMtiktok": {
        "regex": r"https?:\/\/vm.tiktok.com\/[a-zA-Z0-9_-]{9,}",
        "nsfw": False,
    },
    "WEBtiktok": {
        "regex": r"https?:\/\/(www.)?tiktok.com\/@?[a-zA-Z0-9_]{4,}\/video\/[0-9]{1,}",
        "nsfw": False,
    },
    "instagram": {
        "regex": r"https://(www.)?instagram.com/(p|tv|reel)/[a-zA-Z0-9-_]{5,}",
        "nsfw": False,
    },
    "twitch": {"regex": r"https?:\/\/clips.twitch.tv\/[a-zA-Z0-9_-]*", "nsfw": False},
    "twitter": {
        "regex": r"https?:\/\/twitter.com\/[a-zA-Z0-9_]{2,15}\/status\/[0-9]{19}",
        "nsfw": True,
    },
    "reddit": {
        "regex": r"https?:\/\/(www.)reddit.com\/r\/[a-zA-Z0-9_-]{1,20}\/comments\/[a-z0-9]{6}",
        "nsfw": True,
    },
    "youtube_short": {
        "regex": r"https:\/\/(www.)?youtube.com\/shorts\/[a-zA-Z0-9_-]{11}",
        "nsfw": False,
    },
    "youtube": {
        "regex": r"https:\/\/(www.)?youtu(.be|be.com)\/(watch\?v=[a-zA-Z0-9_-]{11}|[a-zA-Z0-9_-]{11})",
        "nsfw": False,
    },
    "pornhub": {
        "regex": r"(https://)?(www.)?pornhub.com/view_video.php\?viewkey=[a-zA-Z0-9]{0,20}",
        "nsfw": True,
    },
}


async def get_video(
    ctx: GuildContext | Context, url: str, auto: bool = False
) -> Optional[str]:
    for regex in video_regexes:
        result = re.search(video_regexes[regex]["regex"], url)
        if result:
            if video_regexes[regex]["nsfw"]:
                if not ctx.channel.is_nsfw():
                    msg = "The site given has been marked as NSFW, please switch to a NSFW channel."
                    if auto:
                        await ctx.send(msg)
                        return

                    raise commands.BadArgument(msg)

            return result.group(0)

    return None


def natural_size(size_in_bytes: int) -> str:
    """
    Converts a number of bytes to an appropriately-scaled unit
    E.g.:
        1024 -> 1.00 KiB
        12345678 -> 11.77 MiB
    """
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB")

    power = int(math.log(max(abs(size_in_bytes), 1), 1024))

    return f"{size_in_bytes / (1024 ** power):.2f} {units[power]}"


async def run(cmd: str) -> Optional[str]:

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await proc.communicate()

    if stdout:
        return f"[stdout]\n{stdout.decode()}"

    if stderr:
        raise TypeError(f"[stderr]\n{stderr.decode()}")


class GuildContext(Context):
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


def human_join(seq: Sequence[str], delim=", ", final="or", spaces: bool = True) -> str:
    size = len(seq)
    if size == 0:
        return ""

    if size == 1:
        return seq[0]

    if size == 2:
        return f"{seq[0]} {final} {seq[1]}"

    final = f" {final} " if spaces else final
    return delim.join(seq[:-1]) + f"{final}{seq[-1]}"


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


def response_checker(response: ClientResponse) -> bool:
    if response.status == 200:
        return True
    elif response.status == 502:
        raise BadGateway("The server is down or under maintenance, try again later.")
    elif response.status == 404:
        raise NotFound("The requested resource could not be found.")
    elif response.status == 400:
        raise BadRequest("The request was invalid.")
    elif response.status == 401:
        raise Unauthorized("The request requires authentication.")
    elif response.status == 403:
        raise Forbidden("The request was forbidden.")
    elif str(response.status).startswith("5"):
        reason = (
            f"\nReason: {textwrap.shorten(response.reason, 100)}"
            if response.reason
            else ""
        )
        raise ServerErrorResponse(
            f"The server returned an error ({response.status}). {reason}"
        )
    else:
        raise ResponseError(
            f"Something went wrong, try again later? \nStatus code: `{response.status}`"
        )
