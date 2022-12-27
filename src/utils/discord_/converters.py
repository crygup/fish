from __future__ import annotations

import imghdr
import pathlib
import random
import re
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Generic,
    List,
    Optional,
    Tuple,
    Type,
    TypeAlias,
    TypeVar,
    Union,
)
from aiohttp.client_exceptions import InvalidURL

import discord
from braceexpand import UnbalancedBracesError, braceexpand  # type: ignore
from bs4 import BeautifulSoup
from discord.ext import commands
from discord.ext.commands import FlagConverter
from ossapi.ossapiv2 import Beatmap, Beatmapset, User
from steam.steamid import steam64_from_url
from wand.color import Color

from ..vars.errors import InvalidColor, NotTenorUrl, UnknownAccount
from ..helpers import get_lastfm, get_roblox, get_twemoji, to_bytesio, to_thread
from ..helpers.roblox import fetch_user_id_by_name
from ..vars import (
    OSU_BEATMAP_RE,
    OSU_BEATMAPSET_RE,
    OSU_ID_RE,
    TENOR_PAGE_RE,
    default_headers,
    BlankException,
)

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context

FCT = TypeVar("FCT", bound="FlagConverter")

Argument: TypeAlias = Optional[
    discord.User
    | discord.Emoji
    | discord.PartialEmoji
    | discord.Role
    | discord.Message
    | str
]


class OsuAccountConverter(commands.Converter):
    """Converts text to an osu accounts"""

    @to_thread
    def get_user(self, ctx: Context, account) -> User:
        return ctx.bot.osu.user(account)

    async def convert(self, ctx: Context, argument: Union[discord.User, str]) -> User:
        bot = ctx.bot

        if not isinstance(argument, str):
            account = await bot.redis.hget(f"accounts:{argument.id}", "osu")
            if not account:
                raise UnknownAccount(f"{argument} has not set an osu! accuont yet.")

            argument = account
        else:
            argument = re.sub(r"https://osu.ppy.sh/users/", "", argument)

        return await self.get_user(ctx, argument)


class ColorConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> Color:
        try:
            return Color(argument.strip())
        except ValueError as exc:
            raise InvalidColor(f"`{argument}` is not a valid color") from exc


class RobloxAccountConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> int:
        if argument.isdigit():
            return int(argument)

        try:
            _user = await commands.UserConverter().convert(ctx, argument)
            user = await get_roblox(ctx.bot, _user.id)
        except commands.UserNotFound:
            user = None

        return await fetch_user_id_by_name(
            ctx.bot.session, argument if user is None else user
        )


class RobloxAssetConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> int:
        pattern = re.compile(r"(https://www.roblox.com/catalog/)?(?P<id>[0-9]{5,})")
        results = pattern.search(argument)

        if results is None:
            raise commands.BadArgument(f"`{argument}` is not a valid asset ID")

        return int(results.group("id"))


class ImageConverter(commands.Converter):
    """
    Converts to a BytesIO image
    """

    async def from_embed(self, ctx: Context, embed: discord.Embed) -> BytesIO:
        if embed.image and embed.image.url:
            return await to_bytesio(ctx.session, embed.image.url)

        if embed.thumbnail and embed.thumbnail.url:
            return await to_bytesio(ctx.session, embed.thumbnail.url)

        raise BlankException("No image found in embed")

    async def from_str(self, ctx: Context, content: str) -> BytesIO:
        emoji = await get_twemoji(ctx.session, content)
        if emoji:
            return BytesIO(emoji)

        try:
            url = await TenorUrlConverter().convert(ctx, content)
        except NotTenorUrl:
            url = content

        try:
            async with ctx.session.get(url) as resp:
                if resp.ok:
                    accepted_types = ["gif", "png", "jpeg", "webp"]
                    image = BytesIO(await resp.read())
                    what = imghdr.what(image)  # type: ignore
                    if what in accepted_types:
                        return image
        except InvalidURL:
            pass

        raise BlankException("Couldn't convert image")

    async def from_message(self, ctx: Context, message: discord.Message, skip_author: bool = False) -> BytesIO:  # type: ignore
        if message.attachments:
            return BytesIO(await message.attachments[0].read())

        if message.embeds:
            for embed in message.embeds:
                try:
                    image = await self.from_embed(ctx, embed)
                except BlankException:
                    pass
                else:
                    return image

        if message.content:
            try:
                await self.from_str(ctx, message.content)
            except BlankException:
                pass

        if not skip_author:
            return BytesIO(await message.author.display_avatar.replace(size=512).read())

        raise BlankException("Could not get image from message")

    async def convert(self, ctx: Context, argument: Argument) -> BytesIO:
        message = ctx.message

        if message.attachments:
            return BytesIO(await message.attachments[0].read())

        if argument is None:
            async for msg in ctx.channel.history(limit=10, before=message):
                try:
                    return await self.from_message(ctx, msg, True)
                except BlankException:
                    continue

        if isinstance(argument, discord.User):
            return BytesIO(await argument.display_avatar.replace(size=512).read())

        if isinstance(argument, (discord.Emoji, discord.PartialEmoji)):
            try:
                return BytesIO(await argument.read())
            except ValueError:
                pass

        if isinstance(argument, discord.Role):
            if argument.display_icon and not isinstance(argument.display_icon, str):
                return BytesIO(await argument.display_icon.read())

        if isinstance(argument, str):
            try:
                await self.from_str(ctx, argument)
            except BlankException:
                pass

        return BytesIO(await message.author.display_avatar.replace(size=512).read())


class LastfmTimeConverter(commands.Converter):
    """
    Converts time to lastfm time
    """

    async def convert(self, ctx: Context, argument: str) -> str:
        response = "7day"

        if re.search("7d|weekly|week", argument, re.IGNORECASE):
            response = "7day"
        elif re.search("1mon|1m|monthy|m", argument, re.IGNORECASE):
            response = "1month"
        elif re.search("3mon|3m|quartery|q", argument, re.IGNORECASE):
            response = "3month"
        elif re.search("6mon|6m|halfy|h", argument, re.IGNORECASE):
            response = "6month"
        elif re.search("12mon|12m|yeary|y", argument, re.IGNORECASE):
            response = "12month"

        return response


class BeatmapConverter(commands.Converter):
    """
    Converts beatmaps
    """

    @to_thread
    def get_beatmap(self, ctx: Context, beatmapid: int) -> Beatmap:
        return ctx.bot.osu.beatmap(beatmapid)

    async def convert(self, ctx: Context, argument: str) -> Beatmap:
        beatmapset_check = OSU_BEATMAPSET_RE.search(argument)
        if beatmapset_check:
            return await self.get_beatmap(ctx, int(beatmapset_check.group("map")))

        beatmap_check = OSU_BEATMAP_RE.search(argument)
        if beatmap_check:
            return await self.get_beatmap(ctx, int(beatmap_check.group("id")))

        id_check = OSU_ID_RE.search(argument)
        if id_check:
            return await self.get_beatmap(ctx, int(id_check.group("id")))

        raise ValueError("Unknown beatmap")


class BeatmapsetConverter(commands.Converter):
    """
    Converts beatmapsets
    """

    @to_thread
    def get_beatmapset(self, ctx: Context, beatmapsetid: int) -> Beatmapset:
        return ctx.bot.osu.beatmapset(beatmapsetid)

    async def convert(self, ctx: Context, argument: str) -> Beatmapset:
        beatmapset_check = OSU_BEATMAPSET_RE.search(argument)
        if beatmapset_check:
            return await self.get_beatmapset(ctx, int(beatmapset_check.group("map")))

        id_check = OSU_ID_RE.search(argument)
        if id_check:
            return await self.get_beatmapset(ctx, int(id_check.group("id")))

        raise ValueError("Unknown beatmapset")


class LastfmConverter(commands.Converter):
    """
    Converts last.fm usernames
    """

    async def convert(self, ctx: Context, argument: str) -> str:
        if argument.lower().startswith("fm:"):
            return argument[3:]

        try:
            user = await commands.UserConverter().convert(ctx, argument)
        except commands.UserNotFound:
            return argument

        name = await get_lastfm(ctx.bot, user.id)

        return name


def SteamIDConverter(account: str) -> int:
    profiles = re.search(
        r"https:\/\/steamcommunity.com\/profiles\/(?P<id>[0-9]{17})", account
    )

    actual_id = re.search(r"[0-9]{17}", account)

    id_url = re.search(
        r"https:\/\/steamcommunity.com\/id\/(?P<id>[a-zA-Z0-9_-]{2,32})", account
    )

    name = re.search(r"[a-zA-Z0-9_-]{2,32}", account)

    if profiles is not None:
        account = steam64_from_url(
            f'https://steamcommunity.com/profiles/{profiles.group("id")}'  # type: ignore
        )

    elif id_url is not None:
        account = steam64_from_url(
            f'https://steamcommunity.com/id/{id_url.group("id")}'  # type: ignore
        )

    elif actual_id is not None:
        account = actual_id.group(0)

    elif name is not None:
        account = steam64_from_url(f"https://steamcommunity.com/id/{name.group(0)}")  # type: ignore

    else:
        raise UnknownAccount("Invalid username.")

    if account is None:
        raise UnknownAccount("No account found.")

    return int(account)


class SteamConverter(commands.Converter):
    """
    Converts steam account
    """

    async def convert(
        self, ctx: Context, argument: Union[discord.User, discord.Member, str]
    ) -> int:
        if not isinstance(argument, str):
            results = await ctx.bot.redis.hget(f"accounts:{argument.id}", "steam")

            if not results:
                raise UnknownAccount("No account found for this user.")

        user_id = (
            await ctx.bot.redis.hget(f"accounts:{argument.id}", "steam")
            if not isinstance(argument, str)
            else argument
        )

        return SteamIDConverter(user_id)


def find_extensions_in(path: Union[str, pathlib.Path]) -> list:
    """
    Tries to find things that look like bot extensions in a directory.
    """

    if not isinstance(path, pathlib.Path):
        path = pathlib.Path(path)

    if not path.is_dir():
        return []

    extension_names = []

    # Find extensions directly in this folder
    for subpath in path.glob("*.py"):
        parts = subpath.with_suffix("").parts
        if parts[0] == ".":
            parts = parts[1:]

        extension_names.append(".".join(parts))

    # Find extensions as subfolder modules
    for subpath in path.glob("*/__init__.py"):
        parts = subpath.parent.parts
        if parts[0] == ".":
            parts = parts[1:]

        extension_names.append(".".join(parts))

    return extension_names


def resolve_extensions(bot: Bot, name: str) -> list:
    """
    Tries to resolve extension queries into a list of extension names.
    """

    exts = []
    for ext in braceexpand(name):
        if ext.endswith(".*"):
            module_parts = ext[:-2].split(".")
            path = pathlib.Path(*module_parts)
            exts.extend(find_extensions_in(path))
        elif ext in ["~", "all"]:
            exts.extend(bot.extensions)
        elif ext.startswith("."):
            exts.append(f"cogs{ext}")
        else:
            exts.append(ext)

    return exts


class ExtensionConverter(commands.Converter):  # pylint: disable=too-few-public-methods
    """
    A converter interface for resolve_extensions to match extensions from users.
    """

    async def convert(self, ctx: Context, argument) -> list:
        try:
            return resolve_extensions(ctx.bot, argument)
        except UnbalancedBracesError as exc:
            raise commands.BadArgument(str(exc))


class RoleConverter(commands.Converter[discord.Role]):
    """Converts argument to a `discord.Role`."""

    async def convert(self, ctx: Context, argument: str) -> discord.Role:
        if argument.lower() == "random":
            if ctx.guild is None:
                raise commands.GuildNotFound("No guild found")
            role = random.choice(ctx.guild.roles)

        elif argument.lower() == "me":
            if not isinstance(ctx.author, discord.Member):
                raise TypeError("You must be a member to use this command.")
            role = ctx.author.top_role

        else:
            try:
                return await commands.RoleConverter().convert(ctx, argument)
            except commands.RoleNotFound:
                if ctx.guild is None:
                    raise commands.GuildNotFound("No guild found")
                role = discord.utils.find(
                    lambda r: r.name.lower() == argument.lower(),
                    ctx.guild.roles,
                )

        if role is None:
            raise commands.RoleNotFound(argument)

        return role


class EmojiConverter(commands.Converter):
    """Converts discord.Message to List[discord.PartialEmoji]"""

    async def from_message(
        self, ctx: Context, message: str
    ) -> List[discord.PartialEmoji]:
        custom_emoji = re.compile(
            r"<(?P<a>a)?:(?P<name>[a-zA-Z0-9_~]{1,}):(?P<id>[0-9]{15,19})>"
        )
        real_emojis: Optional[List[Tuple[str, str, str]]] = custom_emoji.findall(
            message
        )

        if not real_emojis:
            raise TypeError("No emojis found.")

        emojis: List[discord.PartialEmoji] = []
        for emoji in real_emojis:
            try:
                emoji = await commands.PartialEmojiConverter().convert(
                    ctx, f"<{emoji[0]}:{emoji[1]}:{emoji[2]}>"
                )
            except commands.PartialEmojiConversionFailure:
                continue
            emojis.append(emoji)

        return emojis


class UntilFlag(Generic[FCT]):
    def __init__(self, value: str, flags: FCT) -> None:
        self.value = value
        self.flags = flags
        self._regex = self.flags.__commands_flag_regex__  # type: ignore

    def __class_getitem__(cls, item: Type[FlagConverter]) -> UntilFlag:
        return cls(value="...", flags=item())

    def validate_value(self, argument: str) -> bool:
        stripped = argument.strip()
        if not stripped:
            raise commands.BadArgument(f"No body has been specified before the flags.")
        return True

    async def convert(self, ctx: commands.Context, argument: str) -> UntilFlag:
        value = self._regex.split(argument, maxsplit=1)[0]
        if not await discord.utils.maybe_coroutine(self.validate_value, argument):
            raise commands.BadArgument("Failed to validate argument preceding flags.")
        flags = await self.flags.convert(ctx, argument=argument[len(value) :])
        return UntilFlag(value=value, flags=flags)


class TenorUrlConverter(commands.Converter):
    @to_thread
    def get_real_url(self, text: str) -> str:
        scraper = BeautifulSoup(text, "html.parser")
        container = scraper.find(id="single-gif-container")
        if not container:
            raise ValueError("Couldn't find anything.")

        try:
            element = container.find("div").find("div").find("img")  # type: ignore
        except Exception as e:
            raise ValueError(f"Something went wrong. \n{e}")

        if element is None:
            raise ValueError(f"Something went wrong.")

        return element["src"]  # type: ignore

    async def convert(self, ctx: commands.Context[Bot], url: str) -> str:
        real_url = TENOR_PAGE_RE.search(url)

        if not real_url:
            raise NotTenorUrl("Invalid Tenor URL.")

        async with ctx.bot.session.get(
            real_url.group(0), headers=default_headers
        ) as resp:
            text = await resp.text()

        url = await self.get_real_url(text)

        return url


class BoolConverter(commands.Converter):
    """Converts discord.Message to List[discord.PartialEmoji]"""

    async def convert(self, ctx: Context, message: str) -> bool:
        compiled = re.compile(r"(yes|y|true)", re.IGNORECASE)

        return bool(compiled.match(message))
