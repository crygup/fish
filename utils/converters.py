from __future__ import annotations

import asyncio
import re
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Union

from bs4 import BeautifulSoup
import discord
from discord.ext import commands
from .functions import response_checker, to_thread
from .regexes import TENOR_PAGE_RE, KLIPY_RE
from .vars import base_header

if TYPE_CHECKING:
    from extensions.context import Context

SVG_URL = (
    "https://raw.githubusercontent.com/twitter/twemoji/master/assets/svg/{chars}.svg"
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
    @to_thread
    def get_url(self, text: str) -> str:
        scraper = BeautifulSoup(text, "html.parser")
        video = scraper.find("video", class_="w-full h-full object-contain")
        if not video:
            raise commands.BadArgument("Couldn't find video on that page.")
        src = video.get("src")
        if not src:
            raise commands.BadArgument("Video element has no src attribute.")
        return src if src.startswith("http") else f"https:{src}"

    async def convert(self, ctx: Context, url: str) -> str:
        if not KLIPY_RE.search(url):
            raise commands.BadArgument("Invalid Klipy URL.")
        async with ctx.session.get(url) as r:
            text = await r.text()
        return await self.get_url(text)


class MediaConverter(commands.Converter[str]):
    """Converts user input into a media URL.

    Checks in order:
    1. Attachments on the command message
    2. Replied message (attachments, embeds, stickers)
    3. Mentioned user → display avatar
    4. Tenor link
    5. Direct image/video URL
    """

    async def convert(self, ctx: Context, argument: str = "") -> str:
        if ctx.message.attachments:
            att = ctx.message.attachments[0]
            if att.content_type and (
                att.content_type.startswith("image/")
                or att.content_type.startswith("video/")
            ):
                return att.url

        # 2. replied message
        ref = ctx.message.reference
        if ref and ref.message_id:
            try:
                replied = await ctx.fetch_message(ref.message_id)
            except discord.HTTPException:
                replied = None
            if replied:
                if replied.attachments:
                    att = replied.attachments[0]
                    if att.content_type and (
                        att.content_type.startswith("image/")
                        or att.content_type.startswith("video/")
                    ):
                        return att.url
                if replied.embeds:
                    emb = replied.embeds[0]
                    if emb.image and emb.image.url:
                        return emb.image.url
                    if emb.thumbnail and emb.thumbnail.url:
                        return emb.thumbnail.url
        # 2.5. scan recent messages for media
        if not argument:
            try:
                async for msg in ctx.history(limit=6):
                    if msg.id == ctx.message.id:
                        continue
                    if msg.attachments:
                        att = msg.attachments[0]
                        if att.content_type and (
                            att.content_type.startswith("image/")
                            or att.content_type.startswith("video/")
                        ):
                            return att.url
                    if msg.embeds:
                        emb = msg.embeds[0]
                        if emb.image and emb.image.url:
                            return emb.image.url
                        if emb.thumbnail and emb.thumbnail.url:
                            return emb.thumbnail.url
                    # also check message content for direct media URLs
                    lowered = msg.content.lower()
                    if any(
                        ext in lowered
                        for ext in (
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".gif",
                            ".webp",
                            ".mp4",
                            ".webm",
                            ".mov",
                        )
                    ):
                        # find the actual URL
                        for word in msg.content.split():
                            if word.startswith(("http://", "https://")):
                                base = word.lower().split("?")[0]
                                if any(
                                    base.endswith(e)
                                    for e in (
                                        ".png",
                                        ".jpg",
                                        ".jpeg",
                                        ".gif",
                                        ".webp",
                                        ".mp4",
                                        ".webm",
                                        ".mov",
                                    )
                                ):
                                    return word
            except discord.HTTPException:
                pass

        # 4. tenor link
        if argument:
            try:
                return await TenorUrlConverter().convert(ctx, argument)
            except commands.BadArgument:
                pass

        # 4b. klipy link
        if argument:
            try:
                return await KlipyUrlConverter().convert(ctx, argument)
            except commands.BadArgument:
                pass

        # 5. direct image/video URL — only trust known extensions
        if argument:
            base = argument.lower().split("?")[0]
            for ext in (
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".mp4",
                ".webm",
                ".mov",
            ):
                if base.endswith(ext):
                    return argument


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

        # Not a user — validate as raw username.
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

        # Not a Discord user — resolve raw input to SteamID64.
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
