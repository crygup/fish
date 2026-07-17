from __future__ import annotations

import asyncio
import re
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union
from urllib.parse import quote, unquote, urlsplit

import aiohttp
from bs4 import BeautifulSoup
import discord
from discord.ext import commands
from .functions import response_checker, to_thread
from .regexes import TENOR_PAGE_RE
from .vars import base_header

if TYPE_CHECKING:
    from extensions.context import Context

SVG_URL = (
    "https://raw.githubusercontent.com/twitter/twemoji/master/assets/svg/{chars}.svg"
)
MEDIA_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".webm",
    ".mov",
)


class LastfmTimeConverter(commands.Converter):
    async def convert(self, _, argument: str) -> str:
        response = "overall"

        if re.search("7d|weekly|week", argument, re.IGNORECASE):
            response = "7day"
        elif re.search("1mon|1m|monthy|m", argument, re.IGNORECASE):
            response = "1month"
        elif re.search("3mon|3m|quarterly|q", argument, re.IGNORECASE):
            response = "3month"
        elif re.search(
            "6mon|6m|half-yearly|halfyearly|half|h", argument, re.IGNORECASE
        ):
            response = "6month"
        elif re.search("12mon|12m|yeary|y", argument, re.IGNORECASE):
            response = "12month"

        return response


class URLConverter(commands.Converter[str]):
    async def convert(self, ctx: Context, argument: str) -> str:
        if not re.match(r"^https?://", argument):
            argument = f"http://{argument}"

        return argument


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
            raise commands.BadArgument("No info found for this query")

        return data

    async def search_album(self, query: str) -> str:
        data = await self.search_raw(query)

        return data[0]["external_urls"]["spotify"]

    async def search_artist(self, query: str) -> str:
        data = await self.search_raw(query)

        return data[0]["external_urls"]["spotify"]

    async def search_track(self, query: str) -> str:
        data = await self.search_raw(query)

        return data[0]["external_urls"]["spotify"]


async def render_with_rsvg(blob):
    rsvg = "rsvg-convert --width=1024"
    proc = await asyncio.create_subprocess_shell(
        rsvg,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(blob)
    return BytesIO(stdout), stderr


class TwemojiConverter(commands.Converter):
    """Converts str to twemoji bytesio"""

    async def convert(self, ctx: Context, argument: str) -> BytesIO:
        if len(argument) >= 8:
            raise commands.BadArgument("Too long to be an emoji")

        VS_16 = "\N{VARIATION SELECTOR-16}"

        resp = None
        blob = b""
        while not resp or resp.status != 200:
            chars = "-".join(f"{ord(c):x}" for c in argument)
            async with ctx.bot.session.get(SVG_URL.format(chars=chars)) as resp:
                if resp.status != 200:
                    if VS_16 in argument:
                        new_ipt = argument.removeprefix(VS_16)
                        if new_ipt == argument:
                            new_ipt = argument.replace(VS_16, "")
                        ipt = new_ipt
                        continue
                    raise commands.BadArgument("Not a valid unicode emoji.")
                blob = await resp.read()

        converted, stderr = await render_with_rsvg(blob)

        if stderr:
            raise Exception(stderr.decode())

        return converted


class TenorUrlConverter(commands.Converter):
    @to_thread
    def get_url(self, text: str) -> str:
        scraper = BeautifulSoup(text, "html.parser")
        container = scraper.find(id="single-gif-container")

        if not container:
            raise commands.BadArgument("Couldn't find anything.")

        try:
            element = container.find("div").find("div").find("img")  # type: ignore
        except Exception as e:
            raise commands.BadArgument(f"Something went wrong. \n{e}")

        if element is None:
            raise commands.BadArgument(f"Something went wrong.")

        return element["src"]  # type: ignore

    async def convert(self, ctx: Context, url: str) -> str:
        TUrl = TENOR_PAGE_RE.search(url)

        if not TUrl:
            raise commands.BadArgument("Invalid Tenor URL.")

        async with ctx.session.get(TUrl.group(0), headers=base_header) as r:
            text = await r.text()

        url = await self.get_url(text)

        return re.sub("AAAAd", "AAAAC", url)


class KlipyUrlConverter(commands.Converter):
    def __init__(self, media_format: str = "gif") -> None:
        self.media_format = media_format

    @staticmethod
    def _slug(url: str) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.hostname or "").lower().removeprefix("www.") != "klipy.com"
        ):
            raise commands.BadArgument("Invalid Klipy URL.")
        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        if len(parts) != 2 or parts[0].lower() != "gifs":
            raise commands.BadArgument("Invalid Klipy GIF URL.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
            raise commands.BadArgument("Invalid Klipy GIF slug.")
        return parts[1]

    @staticmethod
    def _api_media(payload: object, media_format: str) -> str | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        files = data.get("file") if isinstance(data, dict) else None
        if not isinstance(files, dict):
            return None

        formats = [media_format]
        if media_format == "mp3":
            formats.append("mp4")
        formats.extend(
            format_name
            for format_name in ("mp4", "gif", "webm")
            if format_name not in formats
        )

        for quality in ("hd", "md", "sm", "xs"):
            variants = files.get(quality)
            if not isinstance(variants, dict):
                continue
            for format_name in formats:
                item = variants.get(format_name)
                media_url = item.get("url") if isinstance(item, dict) else None
                parsed = urlsplit(media_url) if isinstance(media_url, str) else None
                if (
                    parsed
                    and parsed.scheme == "https"
                    and parsed.hostname == "static.klipy.com"
                ):
                    return media_url
        return None

    @to_thread
    def get_url(self, text: str) -> str:
        scraper = BeautifulSoup(text, "html.parser")
        candidates: list[str] = []
        for video in scraper.find_all("video"):
            if src := video.get("src"):
                candidates.append(src)
            candidates.extend(
                source.get("src")
                for source in video.find_all("source")
                if source.get("src")
            )
        for meta in scraper.find_all("meta"):
            if meta.get("property") in {"og:video", "og:video:url", "og:image"}:
                if content := meta.get("content"):
                    candidates.append(content)
        for candidate in candidates:
            if candidate.startswith("//"):
                candidate = f"https:{candidate}"
            if candidate.startswith(("http://", "https://")):
                return candidate
        raise commands.BadArgument("Couldn't find media on that Klipy page.")

    async def convert(self, ctx: Context, url: str) -> str:
        slug = self._slug(url)
        api_url = f"https://api.klipy.com/api/v1/gifs/{quote(slug, safe='')}"
        try:
            async with ctx.session.get(api_url, headers=base_header) as response:
                if response.status == 200:
                    direct_url = self._api_media(
                        await response.json(content_type=None), self.media_format
                    )
                    if direct_url:
                        return direct_url
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass

        # Keep an HTML fallback for pages that expose media metadata without a
        # Cloudflare challenge.
        async with ctx.session.get(url, headers=base_header) as response:
            return await self.get_url(await response.text())


class MediaConverter(commands.Converter[str]):
    """Converts user input into a media URL.

    Checks in order:
    1. Attachments on the command message
    2. Replied message (attachments, embeds, stickers)
    3. Mentioned user → display avatar
    4. Tenor link
    5. Direct image/video URL
    """

    @staticmethod
    def _is_media_url(url: object) -> bool:
        return isinstance(url, str) and url.startswith(("http://", "https://"))

    @classmethod
    def _attachment_url(cls, attachment: object) -> str | None:
        url = getattr(attachment, "url", None)
        content_type = getattr(attachment, "content_type", None)
        if (
            cls._is_media_url(url)
            and isinstance(content_type, str)
            and (content_type.startswith("image/") or content_type.startswith("video/"))
        ):
            return url

        filename = str(getattr(attachment, "filename", "")).lower().split("?")[0]
        if cls._is_media_url(url) and filename.endswith(MEDIA_EXTENSIONS):
            return url
        return None

    @classmethod
    def _component_media_url(cls, component: object) -> str | None:
        if isinstance(component, dict):
            media = component.get("media")
            url = media.get("url") if isinstance(media, dict) else None
            nested = component.get("components", ())
            if not nested:
                nested = component.get("items", ())
            accessory = component.get("accessory")
        else:
            media = getattr(component, "media", None)
            url = getattr(media, "url", None)
            nested = getattr(component, "children", None)
            if nested is None:
                nested = getattr(component, "items", ())
            accessory = getattr(component, "accessory", None)

        if cls._is_media_url(url):
            return url
        for child in nested or ():
            found = cls._component_media_url(child)
            if found:
                return found
        if accessory is not None:
            return cls._component_media_url(accessory)
        return None

    @classmethod
    def _message_media_url(cls, message: discord.Message) -> str | None:
        for attachment in message.attachments:
            url = cls._attachment_url(attachment)
            if url:
                return url

        for embed in message.embeds:
            if embed.image and cls._is_media_url(embed.image.url):
                return embed.image.url
            if embed.thumbnail and cls._is_media_url(embed.thumbnail.url):
                return embed.thumbnail.url

        for component in getattr(message, "components", ()):
            url = cls._component_media_url(component)
            if url:
                return url
        return None

    @staticmethod
    def _direct_media_url(argument: str) -> str | None:
        base = argument.lower().split("?")[0]
        return argument if base.endswith(MEDIA_EXTENSIONS) else None

    async def convert(
        self,
        ctx: Context,
        argument: str = "",
        *,
        include_message_media: bool = True,
    ) -> str:
        if argument:
            direct_url = self._direct_media_url(argument)
            if direct_url:
                return direct_url

            for converter in (TenorUrlConverter(), KlipyUrlConverter()):
                try:
                    return await converter.convert(ctx, argument)
                except commands.BadArgument:
                    pass

            try:
                user = await commands.UserConverter().convert(ctx, argument)
            except commands.UserNotFound:
                pass
            else:
                return user.display_avatar.url

            try:
                emoji = await commands.PartialEmojiConverter().convert(ctx, argument)
            except commands.BadArgument:
                pass
            else:
                return emoji.url

        if include_message_media and ctx.message.attachments:
            url = self._attachment_url(ctx.message.attachments[0])
            if url:
                return url

        # 2. replied message
        ref = ctx.message.reference
        if include_message_media and ref and ref.message_id:
            try:
                replied = await ctx.fetch_message(ref.message_id)
            except discord.HTTPException:
                replied = None
            if replied:
                url = self._message_media_url(replied)
                if url:
                    return url
        # 2.5. scan recent messages for media
        if include_message_media and not argument:
            try:
                async for msg in ctx.history(limit=6):
                    if msg.id == ctx.message.id:
                        continue
                    url = self._message_media_url(msg)
                    if url:
                        return url
                    # also check message content for direct media URLs
                    lowered = msg.content.lower()
                    if any(ext in lowered for ext in MEDIA_EXTENSIONS):
                        # find the actual URL
                        for word in msg.content.split():
                            if word.startswith(("http://", "https://")):
                                base = word.lower().split("?")[0]
                                if any(base.endswith(e) for e in MEDIA_EXTENSIONS):
                                    return word
            except discord.HTTPException:
                pass

        raise commands.BadArgument("No image or video found.")


class _AccountConverter(commands.Converter[str]):
    """Base: try to resolve as Discord user → linked account, else validate raw."""

    column: str = ""
    site_name: str = ""
    regex: re.Pattern[str] | None = None
    regex_error: str = "Invalid username."

    async def convert(self, ctx: Context, argument: str) -> str:
        # Try to resolve as a Discord user mention/ID.
        try:
            user = await commands.UserConverter().convert(ctx, argument)
            row = await ctx.bot.pool.fetchrow(
                f'SELECT "{self.column}" FROM accounts WHERE user_id = $1', user.id
            )
            if row and row[self.column]:
                return row[self.column]
            raise commands.BadArgument(
                f"**{user.display_name}** has no linked {self.site_name} account."
            )
        except commands.UserNotFound:
            pass

        # Not a user, validate as raw username.
        value = argument.strip().lower().rstrip("/")
        if self.regex and not self.regex.match(value):
            raise commands.BadArgument(self.regex_error)
        return value


class LastfmConverter(_AccountConverter):
    column = "lastfm"
    site_name = "last.fm"
    regex = re.compile(r"^[a-zA-Z\_\-]{2,15}$")
    regex_error = "Invalid last.fm username. Must be 2-15 characters (letters, underscores, hyphens)."


class LetterboxdConverter(_AccountConverter):
    column = "letterboxd"
    site_name = "Letterboxd"
    regex = re.compile(r"^[a-zA-Z0-9_\-]{2,30}$")
    regex_error = "Invalid Letterboxd username. Must be 2-30 characters (letters, numbers, underscores, hyphens)."

    async def convert(self, ctx: Context, argument: str) -> str:
        if isinstance(argument, (discord.User, discord.Member)):
            row = await ctx.bot.pool.fetchrow(
                'SELECT "letterboxd" FROM accounts WHERE user_id = $1',
                argument.id,
            )
            if row and row["letterboxd"]:
                return row["letterboxd"]
            raise commands.BadArgument(
                f"**{argument.display_name}** has no linked Letterboxd account."
            )
        return await super().convert(ctx, argument)


class SteamConverter(_AccountConverter):
    column = "steam"
    site_name = "Steam"
    regex = re.compile(r"^\d{17}$|^[a-zA-Z0-9_\-]{2,32}$")
    regex_error = (
        "Invalid Steam ID. Provide a SteamID64, profile URL, or custom URL name."
    )

    async def convert(self, ctx: Context, argument: str) -> str:
        # If argument is already a User/Member (slash command pre-conversion).
        if isinstance(argument, (discord.User, discord.Member)):
            row = await ctx.bot.pool.fetchrow(
                "SELECT steam FROM accounts WHERE user_id = $1", argument.id
            )
            if row and row["steam"]:
                return row["steam"]
            raise commands.BadArgument(
                f"**{argument.display_name}** has no linked Steam account."
            )

        # Try to parse as Discord user mention/ID.
        try:
            user = await commands.UserConverter().convert(ctx, argument)
            row = await ctx.bot.pool.fetchrow(
                "SELECT steam FROM accounts WHERE user_id = $1", user.id
            )
            if row and row["steam"]:
                return row["steam"]
            raise commands.BadArgument(
                f"**{user.display_name}** has no linked Steam account."
            )
        except commands.UserNotFound:
            pass

        from .regexes import STEAM_URL_RE, STEAM_ID64_RE

        argument = argument.strip().rstrip("/")
        if m := STEAM_URL_RE.match(argument):
            if sid := m.group(1):
                return sid
            if vanity := m.group(2):
                return await self._resolve_vanity(ctx, vanity)
        if STEAM_ID64_RE.match(argument):
            return argument
        if re.match(r"^[a-zA-Z0-9_\-]{2,32}$", argument):
            return await self._resolve_vanity(ctx, argument)
        raise commands.BadArgument(
            "Invalid Steam profile. Provide a profile URL, SteamID64, or custom URL name."
        )

    @staticmethod
    async def _resolve_vanity(ctx: Context, vanity: str) -> str:
        key = ctx.bot.config["keys"]["steam"]
        url = f"https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?key={key}&vanityurl={vanity}"
        async with ctx.bot.session.get(url) as resp:
            data = await resp.json()
            if sid := data.get("response", {}).get("steamid"):
                return sid
            raise commands.BadArgument(f"No Steam profile found for **{vanity}**.")


class SteamGroupConverter(commands.Converter[str]):
    """Resolves a Discord user (for linked account) or raw Steam group input."""

    async def convert(self, ctx: Context, argument: str) -> str:
        from .regexes import STEAM_GROUP_URL_RE

        argument = argument.strip().rstrip("/")
        name = None
        if m := STEAM_GROUP_URL_RE.match(argument):
            name = m.group(1)
        elif re.match(r"^[a-zA-Z0-9_\-]{2,32}$", argument):
            name = argument
        if not name:
            raise commands.BadArgument(
                "Invalid Steam group. Provide a group URL or custom name."
            )

        # Verify the group exists.
        url = f"https://steamcommunity.com/groups/{name}/memberslistxml/?xml=1"
        async with ctx.bot.session.get(url) as resp:
            if resp.status != 200:
                raise commands.BadArgument(f"No Steam group found for **{name}**.")
            text = await resp.text()
        if "<groupID64>" not in text:
            raise commands.BadArgument(f"No Steam group found for **{name}**.")
        return name
