from __future__ import annotations

import pathlib
import random
import re
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    TypeAlias,
    TypeVar,
    Union,
)

import discord
from aiohttp.client_exceptions import InvalidURL
from braceexpand import UnbalancedBracesError, braceexpand  # type: ignore
from bs4 import BeautifulSoup
from discord.ext import commands
from discord.ext.commands import FlagConverter
from ossapi.ossapiv2 import Beatmap, Beatmapset, User
from steam.steamid import steam64_from_url
from wand.color import Color

from ..helpers import (
    NoTwemojiFound,
    SpotifySearchData,
    fetch_user_id_by_name,
    get_lastfm,
    get_recent_track,
    get_roblox,
    get_twemoji,
    response_checker,
    to_bytesio,
    to_thread,
    what,
)
from ..vars import (
    OSU_BEATMAP_RE,
    OSU_BEATMAPSET_RE,
    OSU_ID_RE,
    TENOR_PAGE_RE,
    BlankException,
    NoImageFound,
    default_headers,
)
from ..vars.errors import InvalidColor, NotTenorUrl, UnknownAccount

if TYPE_CHECKING:
    from bot import Bot
    from cogs.context import Context

FCT = TypeVar("FCT", bound="FlagConverter")

Argument: TypeAlias = Optional[
    discord.Attachment
    | discord.Member
    | discord.User
    | discord.Emoji
    | discord.PartialEmoji
    | discord.Role
    | discord.Message
    | str
]


class ImageConverter:
    def __init__(self, ctx: Context):
        self.accepted_file_types: List[str] = ["png", "gif", "jpg", "jpeg", "webp"]
        self.ctx = ctx

    """
    Converts to a BytesIO image
    """

    async def from_embed(self, embed: discord.Embed):
        ctx = self.ctx

        if embed.thumbnail.url:
            return to_bytesio(ctx.session, embed.thumbnail.url)

        if embed.image.url:
            return to_bytesio(ctx.session, embed.image.url)

        raise NoImageFound("Couldn't find image in embed.")

    async def from_string(self, string: str) -> BytesIO:
        ctx = self.ctx
        try:
            return await get_twemoji(ctx.session, string)
        except NoTwemojiFound:
            pass

        try:
            url = await TenorUrlConverter().convert(ctx, string)
            return await to_bytesio(ctx.session, url)
        except NotTenorUrl:
            pass

        try:
            async with ctx.session.get(string, headers=default_headers) as resp:
                if resp.ok:
                    data = BytesIO(await resp.read())
                    if what(data) in self.accepted_file_types:
                        return data
        except:
            pass

        raise NoImageFound("Couldn't find image in string.")

    async def from_message(
        self, message: discord.Message, skip_author: bool = True
    ) -> BytesIO:
        ctx = self.ctx

        if message.attachments:
            asset = BytesIO(await message.attachments[0].read())
            if what(asset) in self.accepted_file_types:
                return asset

        if message.embeds:
            for embed in message.embeds:
                try:
                    await self.from_embed(embed)
                except NoImageFound:
                    continue

        try:
            return await self.from_string(message.content)
        except NoImageFound:
            pass

        if not skip_author:
            return BytesIO(await message.author.display_avatar.read())

        raise NoImageFound("Couldn't find image in string.")

    async def convert(self, argument: Argument) -> BytesIO:
        ctx = self.ctx
        message = ctx.message

        if message.reference:
            if message.reference.resolved and not isinstance(
                message.reference.resolved, discord.DeletedReferencedMessage
            ):
                try:
                    return await self.from_message(message.reference.resolved, False)
                except NoImageFound:
                    pass

        if message.attachments:
            return BytesIO(await message.attachments[0].read())

        if isinstance(argument, discord.Attachment):
            return BytesIO(await argument.read())

        if isinstance(argument, (discord.User, discord.Member)):
            return BytesIO(await argument.display_avatar.read())

        if isinstance(argument, discord.Emoji):
            return BytesIO(await argument.read())

        if isinstance(argument, discord.PartialEmoji):
            if argument.is_custom_emoji():
                return BytesIO(await argument.read())

        if isinstance(argument, discord.Role):
            if argument.icon:
                return BytesIO(await argument.icon.read())

        if isinstance(argument, discord.Message):
            try:
                return await self.from_message(argument, False)
            except NoImageFound:
                pass

        if isinstance(argument, str):
            try:
                return await self.from_string(argument)
            except NoImageFound:
                pass

        async for message in ctx.channel.history(limit=10):
            try:
                await self.from_message(message)
            except NoImageFound:
                continue

        return BytesIO(await ctx.author.display_avatar.read())


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


class LastfmTimeConverter(commands.Converter):
    """
    Converts time to lastfm time
    """

    async def convert(self, ctx: Context, argument: str) -> str:
        response = "7day"

        if re.match("7d|7day|weekly|week", argument, re.IGNORECASE):
            response = "7day"
        elif re.match("1mon|1m|monthy|m", argument, re.IGNORECASE):
            response = "1month"
        elif re.match("3mon|3m|quartery|q", argument, re.IGNORECASE):
            response = "3month"
        elif re.match("6mon|6m|halfy|h", argument, re.IGNORECASE):
            response = "6month"
        elif re.match("12mon|12m|yeary|y", argument, re.IGNORECASE):
            response = "12month"
        elif re.match("alltime|overall|fulltime|total", argument, re.IGNORECASE):
            response = "overall"

        return response


class LastfmConverter(commands.Converter):
    """
    Converts last.fm usernames
    """

    async def convert(self, ctx: Context, argument: str) -> str:
        if argument.lower().startswith("fm:"):
            name = argument[3:]

        else:
            try:
                user = await commands.UserConverter().convert(ctx, argument)
            except commands.UserNotFound:
                user = None

            if user is None:
                raise commands.UserNotFound(argument)

            name = await get_lastfm(ctx.bot, user.id)

        return name


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

    async def convert(self, ctx: Context, url: str) -> str:
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


class BannedMember(commands.Converter):
    async def convert(self, ctx: Context, argument: str):
        if argument.isdigit():
            member_id = int(argument, base=10)
            try:
                return await ctx.guild.fetch_ban(discord.Object(id=member_id))
            except discord.NotFound:
                raise commands.BadArgument(
                    "This member has not been banned before."
                ) from None

        entity = await discord.utils.find(
            lambda u: str(u.user) == argument, ctx.guild.bans(limit=None)
        )

        if entity is None:
            raise commands.BadArgument("This member has not been banned before.")
        return entity


class SpotifyConverter:
    format_mode = {
        "track": "tracks",
        "album": "albums",
        "artist": "artists",
        "track,album,artist": "albums",
    }

    def __init__(
        self,
        ctx: Context,
        mode: Union[
            Literal["track"], Literal["album"], Literal["artist"], Literal["all"]
        ],
    ):
        super().__init__()
        self.mode = mode if mode != "all" else "track,album,artist"
        self.ctx = ctx

    async def search_raw(self, query: str) -> Dict[Any, Any]:
        ctx = self.ctx
        url = "https://api.spotify.com/v1/search"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.bot.spotify_key}",
        }

        api_data = {"q": query, "type": self.mode, "limit": "10", "market": "US"}

        async with ctx.session.get(url, headers=headers, params=api_data) as resp:
            response_checker(resp)
            data: Optional[Dict[Any, Any]] = (
                (await resp.json()).get(self.format_mode[self.mode]).get(f"items")
            )

        if data == [] or data is None:
            raise BlankException("No info found for this query")

        return data

    async def search_album(
        self, query: str, spotify_data: Optional[SpotifySearchData] = None
    ) -> str:
        data = await self.search_raw(query)

        if spotify_data is None:
            return data[0]["external_urls"]["spotify"]

        for item in data:
            if str(item["name"]).lower() != spotify_data.album.lower():
                continue

            if spotify_data.artist.lower() not in [
                str(artist["name"]).lower() for artist in item["artists"]
            ]:
                continue

            return item["external_urls"]["spotify"]

        raise BlankException("Couldn't find that album.")

    async def search_artist(
        self, query: str, spotify_data: Optional[SpotifySearchData] = None
    ) -> str:
        data = await self.search_raw(query)

        if spotify_data is None:
            return data[0]["external_urls"]["spotify"]

        for item in data:
            if str(item["name"]).lower() != spotify_data.artist.lower():
                continue

            return item["external_urls"]["spotify"]

        raise BlankException("Couldn't find that album.")

    async def search_track(
        self, query: str, spotify_data: Optional[SpotifySearchData] = None
    ) -> str:
        data = await self.search_raw(query)

        if spotify_data is None:
            return data[0]["external_urls"]["spotify"]

        for item in data:
            if str(item["name"]).lower() != spotify_data.track.lower():
                continue

            if str(item["album"]["name"]).lower() != spotify_data.album.lower():
                continue

            if spotify_data.artist.lower() not in [
                str(artist["name"]).lower() for artist in item["artists"]
            ]:
                continue

            return item["external_urls"]["spotify"]

        raise BlankException("Couldn't find that track.")

    async def get_query(
        self, query: Optional[str]
    ) -> Tuple[str, Optional[SpotifySearchData]]:

        ctx = self.ctx
        if query is None:
            name = await get_lastfm(ctx.bot, ctx.author.id)
            track = await get_recent_track(ctx.bot, name)

            query = ""
            if self.mode == "track":
                query = f"{track.name} {track.album.name} {track.artist.name}"

            elif self.mode == "album" or self.mode == "all":
                query = f"{track.album.name} {track.artist.name}"

            elif self.mode == "artist":
                query = track.artist.name

            return query, SpotifySearchData(
                track=track.name,
                album=track.album.name,
                artist=track.artist.name,
            )

        return query, None
