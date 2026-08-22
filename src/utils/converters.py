from __future__ import annotations

import asyncio
import html as html_lib
import re
from collections.abc import Mapping
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union
from urllib.parse import quote, unquote, urljoin, urlsplit

import aiohttp
import discord
import emoji as emoji_lib
from bs4 import BeautifulSoup
from discord.ext import commands

from .downloads import is_discord_media_url, is_downloadable_media_page
from .functions import response_checker, to_thread
from .regexes import TENOR_PAGE_RE
from .vars import base_header

if TYPE_CHECKING:
    from extensions.context import Context

SVG_URL = (
    "https://raw.githubusercontent.com/twitter/twemoji/master/assets/svg/{chars}.svg"
)
TWEMOJI_PNG_URL = (
    "https://raw.githubusercontent.com/jdecked/twemoji/main/" "assets/72x72/{chars}.png"
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
    ".m4v",
    ".mp3",
    ".m4a",
    ".wav",
    ".ogg",
    ".opus",
    ".flac",
    ".aac",
)
MESSAGE_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


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
                (await resp.json()).get(self.format_mode[self.mode]).get("items")
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
    proc = await asyncio.create_subprocess_exec(
        "rsvg-convert",
        "--width=1024",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(blob)
    if proc.returncode:
        raise commands.BadArgument("The emoji image could not be rendered.")
    return BytesIO(stdout), stderr


class TwemojiConverter(commands.Converter):
    """Converts str to twemoji bytesio"""

    @staticmethod
    def is_unicode_emoji(argument: str) -> bool:
        matches = emoji_lib.emoji_list(argument)
        return (
            len(matches) == 1
            and int(matches[0]["match_start"]) == 0
            and int(matches[0]["match_end"]) == len(argument)
        )

    @staticmethod
    def png_url(argument: str) -> str:
        chars = "-".join(
            f"{ord(character):x}"
            for character in argument
            if character != "\N{VARIATION SELECTOR-16}"
        )
        return TWEMOJI_PNG_URL.format(chars=chars)

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
                        argument = new_ipt
                        continue
                    raise commands.BadArgument("Not a valid unicode emoji.")
                blob = await resp.read()

        converted, stderr = await render_with_rsvg(blob)

        if stderr:
            raise Exception(stderr.decode())

        return converted


class TenorUrlConverter(commands.Converter):
    # Tenor serves the original GIF from ``media1.tenor.com`` on many newer
    # pages, while older pages use ``media.tenor.com`` or ``c.tenor.com``.
    # These are all Tenor-owned media hosts, not arbitrary subdomains.
    _MEDIA_HOSTS = {"media.tenor.com", "media1.tenor.com", "c.tenor.com"}
    _DISCORD_PROXY_HOST = re.compile(
        r"^images-ext-\d+\.discordapp\.(?:net|com)$", re.IGNORECASE
    )

    @classmethod
    def media_url_variants(cls, url: str) -> tuple[str, ...]:
        """Return current and legacy URL shapes for a Tenor GIF.

        Tenor has changed the shape of its CDN paths.  Older saved links use
        ``media1.tenor.com/m/<hash>AAAAC/<name>.gif`` while the same media is
        now commonly served as ``media.tenor.com/<hash>AAAAM/<name>.gif``.
        The old URL can remain visible in a browser cache while returning 404
        to a fresh server request, so callers that download media should try
        the known equivalent shapes.
        """

        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if hostname not in cls._MEDIA_HOSTS:
            return (url,)
        path = unquote(parsed.path).strip("/")
        parts = path.split("/")
        if not parts or not parts[-1].casefold().endswith(".gif"):
            return (url,)

        variants: list[str] = [url]
        if parts[0] == "m" and len(parts) >= 3:
            token = parts[1]
            tail = "/".join(parts[2:])
        elif len(parts) >= 2:
            token = parts[0]
            tail = "/".join(parts[1:])
        else:
            return (url,)

        suffixes = ("AAAAM", "AAAAC", "AAAAD")
        base = token[:-5] if token[-5:] in {"AAAAM", "AAAAC", "AAAAD"} else token
        for host in ("media.tenor.com", "media1.tenor.com"):
            for suffix in suffixes:
                candidate = f"https://{host}/{base}{suffix}/{tail}"
                if candidate not in variants:
                    variants.append(candidate)
        return tuple(variants)

    @classmethod
    def _unwrap_discord_proxy(cls, url: str) -> str:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if not cls._DISCORD_PROXY_HOST.fullmatch(hostname):
            return url

        parts = parsed.path.split("/", 3)
        if len(parts) != 4 or parts[1] != "external":
            raise commands.BadArgument("Invalid Discord media proxy URL.")

        embedded = unquote(parts[3])
        if embedded.startswith(("https://", "http://")):
            return embedded

        scheme, separator, remainder = embedded.partition("/")
        if separator and scheme in {"https", "http"} and remainder:
            return f"{scheme}://{remainder}"

        raise commands.BadArgument("Invalid Discord media proxy URL.")

    @classmethod
    async def _direct_gif(cls, ctx: Context, url: str) -> str:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or hostname not in cls._MEDIA_HOSTS:
            raise commands.BadArgument("Invalid Tenor media URL.")

        path = unquote(parsed.path)
        if path.lower().endswith(".gif"):
            return f"https://{hostname}{path}"
        if not path.lower().endswith(".mp4"):
            raise commands.BadArgument("Invalid Tenor media URL.")

        path_parts = path.split("/")
        if len(path_parts) < 3 or "AAAPo" not in path_parts[1]:
            raise commands.BadArgument("Could not find the original Tenor GIF.")

        path_parts[1] = path_parts[1].replace("AAAPo", "AAAAC", 1)
        path_parts[-1] = path_parts[-1].rsplit(".", 1)[0] + ".gif"
        gif_url = f"https://{hostname}{'/'.join(path_parts)}"

        try:
            async with ctx.session.get(
                gif_url,
                headers={**base_header, "Range": "bytes=0-0"},
            ) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if response.status not in {200, 206} or not content_type.startswith(
                    "image/gif"
                ):
                    raise commands.BadArgument("Could not find the original Tenor GIF.")
        except aiohttp.ClientError as exc:
            raise commands.BadArgument(
                "Could not find the original Tenor GIF."
            ) from exc

        return gif_url

    @to_thread
    def get_url(self, text: str) -> str:
        scraper = BeautifulSoup(text, "html.parser")
        container = scraper.find(id="single-gif-container")
        if container is not None:
            try:
                element = container.find("div").find("div").find("img")  # type: ignore
            except (AttributeError, TypeError):
                element = None
            if element is not None:
                source = element.get("src")
                if isinstance(source, str) and source:
                    return source

        # Tenor periodically removes the legacy ``single-gif-container`` from
        # its server-rendered page.  The same original media URL is still
        # exposed through Open Graph, Twitter card, or source metadata, so use
        # those fields as a stable fallback instead of rejecting a valid page.
        candidates: list[str] = []
        for tag in scraper.find_all(["meta", "img", "source", "video"]):
            if tag.name == "meta" and not (
                tag.get("property") in {"og:image", "og:video", "og:video:url"}
                or tag.get("name") in {"twitter:image", "twitter:player:stream"}
                or tag.get("itemprop") in {"contentUrl", "thumbnailUrl"}
            ):
                continue
            source = tag.get("content") or tag.get("src")
            if not isinstance(source, str):
                continue
            source = html_lib.unescape(source).replace("\\/", "/")
            if source.startswith("//"):
                source = f"https:{source}"
            if source.startswith(("https://", "http://")):
                candidates.append(source)
        # Prefer Tenor's original media host over page URLs or thumbnails.
        # Some pages expose both an MP4 preview and a GIF, so prefer the GIF
        # when both are present.
        media_candidates = [
            candidate
            for candidate in candidates
            if (urlsplit(candidate).hostname or "").lower().rstrip(".")
            in self._MEDIA_HOSTS
        ]
        media_candidates.sort(
            key=lambda candidate: 0
            if urlsplit(candidate).path.casefold().endswith(".gif")
            else 1
        )
        if media_candidates:
            return media_candidates[0]
        raise commands.BadArgument("Couldn't find anything.")

    async def convert(self, ctx: Context, url: str) -> str:
        url = self._unwrap_discord_proxy(url.strip())
        parsed = urlsplit(url)
        if (parsed.hostname or "").lower().rstrip(".") in self._MEDIA_HOSTS:
            return await self._direct_gif(ctx, url)

        TUrl = TENOR_PAGE_RE.search(url)

        if not TUrl:
            raise commands.BadArgument("Invalid Tenor URL.")

        async with ctx.session.get(TUrl.group(0), headers=base_header) as r:
            text = await r.text()

        url = re.sub("AAAAd", "AAAAC", await self.get_url(text))
        media_host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if media_host in self._MEDIA_HOSTS:
            # Tenor pages often expose an MP4 preview. Convert its stable
            # media path back to the original GIF rather than submitting the
            # preview as a video to the post library.
            return await self._direct_gif(ctx, url)
        raise commands.BadArgument("Tenor did not expose a usable GIF URL.")


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

        if media_format == "gif":
            # Klipy's pre-optimized GIFs can contain partial-frame updates
            # that render incorrectly in some Discord clients. Prefer the
            # clean MP4 source and let Fishie create a compatible GIF.
            formats = ["mp4", "gif"]
        else:
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
            src = video.get("src")
            if isinstance(src, str):
                candidates.append(src)
            for source in video.find_all("source"):
                source_url = source.get("src")
                if isinstance(source_url, str):
                    candidates.append(source_url)
        for meta in scraper.find_all("meta"):
            if meta.get("property") in {"og:video", "og:video:url", "og:image"}:
                content = meta.get("content")
                if isinstance(content, str):
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
    1. Explicit URL, user, or emoji argument
    2. Attachments on the command message
    3. Replied message media or supported URL in its content
    4. Recent message media or supported URL in its content
    """

    @staticmethod
    def _is_media_url(url: object) -> bool:
        return isinstance(url, str) and url.startswith(("http://", "https://"))

    @staticmethod
    def _message_value(message: object, name: str, default: object = None) -> object:
        """Read a message/snapshot field from Discord objects or raw payloads."""

        if isinstance(message, Mapping):
            return message.get(name, default)
        return getattr(message, name, default)

    @staticmethod
    def _message_sequence(value: object) -> tuple[object, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return ()

    @classmethod
    def _message_snapshots(cls, message: object) -> tuple[object, ...]:
        """Return forwarded-message snapshots, including raw API payloads.

        discord.py exposes snapshots as ``MessageSnapshot`` instances, while
        test fixtures and webhook payloads may still contain the API wrapper
        ``{"message": {...}}``.  Normalize both forms here so every media
        consumer handles forwarded messages consistently.
        """

        raw_snapshots = cls._message_value(message, "message_snapshots", ())
        if isinstance(raw_snapshots, Mapping):
            raw_snapshots = (raw_snapshots,)
        if not isinstance(raw_snapshots, (list, tuple)):
            return ()

        snapshots: list[object] = []
        for snapshot in raw_snapshots:
            if isinstance(snapshot, Mapping):
                snapshot = snapshot.get("message", snapshot)
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots)

    @classmethod
    def _attachment_url(cls, attachment: object) -> str | None:
        if isinstance(attachment, Mapping):
            url = attachment.get("url")
            content_type = attachment.get("content_type")
            filename = attachment.get("filename", "")
        else:
            url = getattr(attachment, "url", None)
            content_type = getattr(attachment, "content_type", None)
            filename = getattr(attachment, "filename", "")
        if (
            cls._is_media_url(url)
            and isinstance(content_type, str)
            and content_type.startswith(("image/", "video/", "audio/"))
        ):
            return url

        filename = str(filename).lower().split("?")[0]
        if cls._is_media_url(url) and filename.endswith(MEDIA_EXTENSIONS):
            return url
        return None

    @classmethod
    def _message_attachments(cls, message: object) -> tuple[object, ...]:
        """Return direct and forwarded-snapshot attachments without duplicates."""

        attachments: list[object] = []
        seen: set[object] = set()

        def add(values: object) -> None:
            if not isinstance(values, (list, tuple)):
                return
            for attachment in values:
                if isinstance(attachment, Mapping):
                    key = attachment.get("id") or attachment.get("url")
                else:
                    key = getattr(attachment, "id", None) or getattr(
                        attachment, "url", None
                    )
                if key is None:
                    key = id(attachment)
                if key in seen:
                    continue
                seen.add(key)
                attachments.append(attachment)

        add(cls._message_value(message, "attachments", ()))
        for snapshot in cls._message_snapshots(message):
            add(cls._message_value(snapshot, "attachments", ()))
        return tuple(attachments)

    @classmethod
    def _component_media_url(cls, component: object) -> str | None:
        if isinstance(component, dict):
            media = component.get("media")
            url = (
                media.get("url")
                if isinstance(media, dict)
                else media if isinstance(media, str) else component.get("url")
            )
            nested = component.get("components") or component.get("items") or ()
            accessory = component.get("accessory")
        else:
            media = getattr(component, "media", None)
            if isinstance(media, str):
                url = media
            else:
                url = getattr(media, "url", None) or getattr(media, "media", None)
            if not isinstance(url, str):
                url = getattr(component, "url", None)
            nested = (
                getattr(component, "children", None)
                or getattr(component, "components", None)
                or getattr(component, "items", ())
            )
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
    def _message_media_urls(
        cls, message: object, *, include_snapshots: bool = True
    ) -> tuple[str, ...]:
        urls: list[str] = []
        seen: set[str] = set()

        def add(url: object) -> None:
            if isinstance(url, str) and cls._is_media_url(url) and url not in seen:
                seen.add(url)
                urls.append(url)

        for attachment in cls._message_attachments(message):
            url = cls._attachment_url(attachment)
            if url:
                add(url)

        for embed in cls._message_sequence(cls._message_value(message, "embeds", ())):
            if isinstance(embed, Mapping):
                image = embed.get("image")
                thumbnail = embed.get("thumbnail")
                add(image.get("url") if isinstance(image, Mapping) else None)
                add(
                    thumbnail.get("url")
                    if isinstance(thumbnail, Mapping)
                    else None
                )
            else:
                image = getattr(embed, "image", None)
                thumbnail = getattr(embed, "thumbnail", None)
                add(getattr(image, "url", None))
                add(getattr(thumbnail, "url", None))

        for component in cls._message_sequence(
            cls._message_value(message, "components", ())
        ):
            url = cls._component_media_url(component)
            if url:
                add(url)
            to_dict = getattr(component, "to_dict", None)
            if callable(to_dict):
                try:
                    url = cls._component_media_url(to_dict())
                except (TypeError, ValueError):
                    url = None
                if url:
                    add(url)
        for sticker in cls._message_sequence(
            cls._message_value(message, "stickers", ())
        ):
            url = (
                sticker.get("url")
                if isinstance(sticker, Mapping)
                else getattr(sticker, "url", None)
            )
            add(url)

        if include_snapshots:
            for snapshot in cls._message_snapshots(message):
                for url in cls._message_media_urls(
                    snapshot, include_snapshots=False
                ):
                    add(url)
        return tuple(urls)

    @classmethod
    def _message_media_url(cls, message: object) -> str | None:
        urls = cls._message_media_urls(message)
        return urls[0] if urls else None

    @staticmethod
    def _direct_media_url(argument: str) -> str | None:
        base = argument.lower().split("?")[0]
        return argument if base.endswith(MEDIA_EXTENSIONS) else None

    @staticmethod
    def _message_urls(content: str) -> tuple[str, ...]:
        return tuple(
            match.group(0).rstrip(".,!?;:'\"`>]}")
            for match in MESSAGE_URL_RE.finditer(content)
        )

    async def _message_content_media_url(
        self,
        ctx: Context,
        message: object,
    ) -> str | None:
        sources = (message, *self._message_snapshots(message))
        seen_content: set[str] = set()
        for source in sources:
            content = self._message_value(source, "content", "")
            if not isinstance(content, str) or content in seen_content:
                continue
            seen_content.add(content)
            for candidate in self._message_urls(content):
                direct = self._direct_media_url(candidate)
                if direct:
                    return direct

                for converter in (TenorUrlConverter(), KlipyUrlConverter()):
                    try:
                        return await converter.convert(ctx, candidate)
                    except commands.BadArgument:
                        pass

                if is_discord_media_url(candidate) or is_downloadable_media_page(
                    candidate
                ):
                    return candidate
        return None

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

            if ctx.guild is not None:
                try:
                    member = await commands.MemberConverter().convert(ctx, argument)
                except commands.MemberNotFound:
                    pass
                else:
                    return member.display_avatar.url

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

            if TwemojiConverter.is_unicode_emoji(argument):
                return TwemojiConverter.png_url(argument)

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
                url = await self._message_content_media_url(ctx, replied)
                if url:
                    return url
                # A replied message without an asset still has a useful
                # author source. Use Discord's resolved display avatar, never
                # the raw default-avatar URL that can be present in payloads.
                display_avatar = getattr(
                    getattr(replied, "author", None), "display_avatar", None
                )
                avatar_url = getattr(display_avatar, "url", None)
                if isinstance(avatar_url, str) and self._is_media_url(avatar_url):
                    return avatar_url
        # 2.5. scan recent messages for media
        if include_message_media and not argument:
            try:
                async for msg in ctx.history(limit=6):
                    if msg.id == ctx.message.id:
                        continue
                    url = self._message_media_url(msg)
                    if url:
                        return url
                    url = await self._message_content_media_url(ctx, msg)
                    if url:
                        return url
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


_LETTERBOXD_HOSTS = {"letterboxd.com", "www.letterboxd.com"}
_BOXD_HOSTS = {"boxd.it", "www.boxd.it"}
_LETTERBOXD_REDIRECTS = {301, 302, 303, 307, 308}
_LETTERBOXD_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,30}$")


def _letterboxd_username(value: str, *, lowercase: bool = True) -> str:
    value = value.strip()
    candidate = value.lower() if lowercase else value
    if not _LETTERBOXD_USERNAME_RE.fullmatch(candidate):
        raise commands.BadArgument(LetterboxdConverter.regex_error)
    return candidate


def _letterboxd_profile_from_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _LETTERBOXD_HOSTS or port is not None:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return None
    # Keep URL path casing intact. boxd.it short codes are case-sensitive.
    return _letterboxd_username(parts[0], lowercase=False)


async def normalize_letterboxd(ctx: Context, argument: str) -> str:
    """Return a validated Letterboxd username from a name or profile link."""
    value = argument.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise commands.BadArgument(
            "Invalid Letterboxd profile. Use a username, profile URL, or boxd.it link."
        ) from error
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"}:
            raise commands.BadArgument(
                "Invalid Letterboxd profile. Use a username, profile URL, or boxd.it link."
            )
        if profile := _letterboxd_profile_from_url(value):
            return profile

        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            host not in _BOXD_HOSTS
            or parsed.username
            or parsed.password
            or port is not None
        ):
            raise commands.BadArgument(
                "Invalid Letterboxd profile. Use a username, profile URL, or boxd.it link."
            )

        current = value
        for _ in range(5):
            try:
                parsed = urlsplit(current)
                port = parsed.port
            except ValueError as error:
                raise commands.BadArgument(
                    "The short link must redirect to a Letterboxd user profile."
                ) from error
            host = (parsed.hostname or "").lower().rstrip(".")
            if host in _LETTERBOXD_HOSTS and port is None:
                if profile := _letterboxd_profile_from_url(current):
                    return profile
                raise commands.BadArgument(
                    "The link must point to a Letterboxd user profile."
                )
            if (
                host not in _BOXD_HOSTS
                or parsed.scheme not in {"http", "https"}
                or parsed.username
                or parsed.password
                or port is not None
            ):
                raise commands.BadArgument(
                    "The short link must redirect to a Letterboxd user profile."
                )
            try:
                async with ctx.bot.session.get(
                    current,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status not in _LETTERBOXD_REDIRECTS:
                        raise commands.BadArgument(
                            "The short link must redirect to a Letterboxd user profile."
                        )
                    location = response.headers.get("Location")
            except commands.BadArgument:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                raise commands.BadArgument(
                    "Could not resolve the Letterboxd short link. Try the full profile URL."
                ) from error
            if not location:
                raise commands.BadArgument(
                    "The short link did not point to a Letterboxd user profile."
                )
            current = urljoin(current, location)
        raise commands.BadArgument(
            "The Letterboxd short link redirected too many times."
        )

    return _letterboxd_username(value)


class LetterboxdConverter(_AccountConverter):
    column = "letterboxd"
    site_name = "Letterboxd"
    regex = _LETTERBOXD_USERNAME_RE
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
        try:
            user = await commands.UserConverter().convert(ctx, argument)
        except commands.UserNotFound:
            return await normalize_letterboxd(ctx, argument)
        row = await ctx.bot.pool.fetchrow(
            'SELECT "letterboxd" FROM accounts WHERE user_id = $1', user.id
        )
        if row and row["letterboxd"]:
            return row["letterboxd"]
        raise commands.BadArgument(
            f"**{user.display_name}** has no linked Letterboxd account."
        )


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

        from .regexes import STEAM_ID64_RE, STEAM_URL_RE

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
