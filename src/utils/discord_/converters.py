from __future__ import annotations

import pathlib
import random
import re
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
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
from aiohttp import ClientResponse
from aiohttp.client_exceptions import InvalidURL
from braceexpand import UnbalancedBracesError, braceexpand  # type: ignore
from bs4 import BeautifulSoup
from discord.ext import commands
from discord.ext.commands import FlagConverter
from ossapi.ossapiv2 import Beatmap, Beatmapset, User
from steam.steamid import steam64_from_url
from wand.color import Color

from ..helpers import (
    SpotifySearchData,
    fetch_user_id_by_name,
    get_lastfm,
    get_recent_track,
    get_roblox,
    response_checker,
    svgbytes_to_btyes,
    to_bytesio,
    to_thread,
    what,
)
from ..vars import (
    IMGUR_PAGE_RE,
    OSU_BEATMAP_RE,
    OSU_BEATMAPSET_RE,
    OSU_ID_RE,
    TENOR_GIF_RE,
    TENOR_PAGE_RE,
    BlankException,
    ImageTooLarge,
    default_headers,
)
from ..vars.errors import InvalidColor, NotTenorUrl, UnknownAccount

if TYPE_CHECKING:
    from re import Match

    from bot import Bot
    from cogs.context import Context

    Argument: TypeAlias = discord.Member | discord.User | discord.PartialEmoji | bytes

FCT = TypeVar("FCT", bound="FlagConverter")


class TwemojiConverter(commands.Converter):
    """Converts str to twemoji bytesio"""

    async def convert(self, ctx: Context, argument: str) -> BytesIO:
        if len(argument) >= 8:
            raise commands.BadArgument("Too long to be an emoji")
        try:
            formatted = "-".join([f"{ord(char):x}" for char in argument])
            url = f"https://raw.githubusercontent.com/twitter/twemoji/abb5a1add2b706520d0d9d6f023297761e64e1c7/assets/svg/{formatted}.svg"
        except Exception:
            raise commands.BadArgument("Couldn't find emoji.")
        else:
            async with ctx.session.get(url) as r:
                if r.ok:
                    return BytesIO(await svgbytes_to_btyes(await r.read()))

            raise commands.BadArgument("Couldn't find emoji.")


class UrlConverter(commands.Converter):
    async def find_tenor_gif(self, ctx: Context, response: ClientResponse) -> bytes:
        bad_arg = commands.BadArgument("An Error occured when fetching the tenor GIF")
        try:
            content = await response.text()
            if match := TENOR_GIF_RE.search(content):
                async with ctx.bot.session.get(match.group()) as gif:
                    if gif.ok:
                        return await gif.read()
                    else:
                        raise bad_arg
            else:
                raise bad_arg
        except Exception:
            raise bad_arg

    async def find_imgur_img(self, ctx: Context, match: Match) -> bytes:
        name = match.group(2)
        raw_url = f"https://i.imgur.com/{name}.gif"

        bad_arg = commands.BadArgument("An Error occured when fetching the imgur GIF")
        try:
            async with ctx.bot.session.get(raw_url) as raw:
                if raw.ok:
                    return await raw.read()
                else:
                    raise bad_arg
        except Exception:
            raise bad_arg

    async def convert(self, ctx: Context, argument: str) -> bytes:

        bad_arg = commands.BadArgument("Invalid image URL")
        argument = argument.strip("<>")
        try:
            async with ctx.bot.session.get(argument) as r:
                if r.ok:
                    if r.content_type.startswith("image/"):
                        byt = await r.read()
                        if r.content_type.startswith("image/svg"):
                            byt = await svgbytes_to_btyes(byt)
                        return byt
                    elif TENOR_PAGE_RE.fullmatch(argument):
                        return await self.find_tenor_gif(ctx, r)
                    elif imgur := IMGUR_PAGE_RE.fullmatch(argument):
                        return await self.find_imgur_img(ctx, imgur)
                    else:
                        raise bad_arg
                else:
                    raise bad_arg
        except Exception:
            raise bad_arg


# https://github.com/Tom-the-Bomb/BombBot/blob/master/bot/utils/imaging/converter.py#L101-L218
class ImageConverter(commands.Converter):
    """
    ImageConverter
    A class for fetching and resolving images within a command, it attempts to fetch, (in order):
        - Member from the command argument, then User if failed
        - A Guild Emoji from the command argument, then default emoji if failed
        - An image url, content-type must be of `image/xx`
        - An attachment from the invocation message
        - A sticker from the invocation message
        If all above fails, it repeats the above for references (replies)
        and also searches for embed thumbnails / images in references
    Raises
    ------
    ImageTooLarge
        The resolved image is too large, possibly a decompression bomb?
    commands.BadArgument
        Failed to fetch anything
    """

    _converters: ClassVar[tuple[type[commands.Converter], ...]] = (
        commands.MemberConverter,
        commands.UserConverter,
        commands.PartialEmojiConverter,
        TwemojiConverter,
        UrlConverter,
    )

    def check_size(self, byt: bytes, *, max_size: int = 15_000_000) -> None:
        if (size := byt.__sizeof__()) > max_size:
            del byt
            raise ImageTooLarge(size, max_size)

    async def converted_to_buffer(self, source: Argument) -> bytes:
        if isinstance(source, (discord.Member, discord.User)):
            source = await source.display_avatar.read()

        elif isinstance(source, discord.PartialEmoji):
            source = await source.read()

        return source

    async def get_attachments(
        self, ctx: Context, message: Optional[discord.Message] = None
    ) -> Optional[bytes]:
        source = None
        message = message or ctx.message

        if files := message.attachments:
            source = await self.get_file_image(files)

        if (st := message.stickers) and source is None:
            source = await self.get_sticker_image(ctx, st)

        if (embeds := message.embeds) and source is None:
            for embed in embeds:
                if img := embed.image.url or embed.thumbnail.url:
                    try:
                        source = await UrlConverter().convert(ctx, img)
                        break
                    except commands.BadArgument:
                        continue
        return source

    async def get_sticker_image(
        self, ctx: Context, stickers: list[discord.StickerItem]
    ) -> Optional[bytes]:
        for sticker in stickers:
            if sticker.format is not discord.StickerFormatType.lottie:
                try:
                    return await UrlConverter().convert(ctx, sticker.url)
                except commands.BadArgument:
                    continue

    async def get_file_image(self, files: list[discord.Attachment]) -> Optional[bytes]:
        for file in files:
            if file.content_type and file.content_type.startswith("image/"):
                byt = await file.read()
                if file.content_type.startswith("image/svg"):
                    byt = await svgbytes_to_btyes(byt)
                return byt

    async def convert(
        self, ctx: Context, argument: str, *, raise_on_failure: bool = True
    ) -> Optional[bytes]:

        for converter in self._converters:
            try:
                source = await converter().convert(ctx, argument)
            except commands.BadArgument:
                continue
            else:
                break
        else:
            if raise_on_failure:
                raise commands.BadArgument("Failed to fetch an image from argument")
            else:
                return None

        return await self.converted_to_buffer(source)

    async def get_image(
        self, ctx: Context, source: Optional[str | bytes], *, max_size: int = 15_000_000
    ) -> BytesIO:

        if isinstance(source, str):
            source = await self.convert(ctx, source, raise_on_failure=False)

        if source is None:
            source = await self.get_attachments(ctx)

            if (ref := ctx.message.reference) and source is None:
                ref = ref.resolved

                if not isinstance(ref, discord.DeletedReferencedMessage) and ref:
                    source = await self.get_attachments(ctx, ref)

                    if source is None and ref.content:
                        source = await self.convert(
                            ctx, ref.content.split()[0], raise_on_failure=False
                        )

        if source is None:
            source = await ctx.author.display_avatar.read()

        self.check_size(source, max_size=max_size)
        return BytesIO(source)


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
        compiled = re.compile(r"(yes|y|true|1)", re.IGNORECASE)

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
