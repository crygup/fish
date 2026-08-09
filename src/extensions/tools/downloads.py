from __future__ import annotations

import re
from io import BytesIO
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit

import discord
from discord import MediaGalleryItem, app_commands, ui
from discord.ext import commands

from core import Cog
from utils import (
    KLIPY_RE,
    TENOR_PAGE_RE,
    Downloader,
    KlipyUrlConverter,
    TemporaryMediaError,
    TenorUrlConverter,
    is_discord_media_url,
    is_downloadable_media_page,
    record_download,
    to_image,
    upload_temporary_media,
)

if TYPE_CHECKING:
    from extensions.context import Context


_MESSAGE_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_DIRECT_MEDIA_SUFFIXES = (
    ".gif",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
)
_DIRECT_MEDIA_HOSTS = frozenset({"media.tenor.com", "c.tenor.com", "static.klipy.com"})
_TENOR_DISCORD_PROXY_RE = re.compile(
    r"^images-ext-\d+\.discordapp\.(?:net|com)$", re.IGNORECASE
)


class DownloadFlags(commands.FlagConverter, delimiter=" ", prefix="-"):
    format: Literal["mp4", "mp3", "webm", "gif"] = commands.flag(
        description="What format to download as (Klipy and Tenor videos are sent as GIFs).",
        default="mp4",
    )
    title: str | None = commands.flag(
        description="The title of the video to save as.",
        default=None,
    )
    ignore_checks: bool = commands.flag(
        description="Owner only. Allow downloads above Discord's upload limit.",
        default=False,
    )
    hidden: bool = commands.flag(
        description="Hides the download (only for app commands)", default=True
    )


class Downloads(Cog):
    async def _message_url(self, ctx: Context, message: discord.Message) -> str | None:
        candidates = [
            match.group(0).rstrip(".,!?;:'\"`]}")
            for match in _MESSAGE_URL_RE.finditer(message.content)
        ]
        for embed in message.embeds:
            if embed.url:
                candidates.append(embed.url)

        for candidate in candidates:
            parsed = urlsplit(candidate)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme.lower() not in {"http", "https"} or is_discord_media_url(
                candidate
            ):
                continue
            if is_downloadable_media_page(candidate):
                return candidate
            if KLIPY_RE.search(candidate) or TENOR_PAGE_RE.search(candidate):
                return candidate
            if hostname in _DIRECT_MEDIA_HOSTS and parsed.path.lower().endswith(
                _DIRECT_MEDIA_SUFFIXES
            ):
                return candidate
            if _TENOR_DISCORD_PROXY_RE.fullmatch(hostname):
                # TenorUrlConverter unwraps Discord's proxy URL and validates
                # that it points to an original Tenor GIF.
                try:
                    await TenorUrlConverter().convert(ctx, candidate)
                except commands.BadArgument:
                    continue
                return candidate
        return None

    async def _find_recent_url(self, ctx: Context) -> str | None:
        """Find the first supported URL in a reply or the five latest messages."""
        current = getattr(ctx, "message", None)
        messages: list[discord.Message] = []
        seen: set[int] = set()

        def add(message: object) -> None:
            if not isinstance(message, discord.Message) or message.id in seen:
                return
            seen.add(message.id)
            messages.append(message)

        reference = getattr(current, "reference", None)
        if reference is not None and getattr(reference, "message_id", None):
            replied = getattr(reference, "resolved", None)
            if not isinstance(replied, discord.Message):
                try:
                    replied = await ctx.fetch_message(reference.message_id)
                except (discord.HTTPException, discord.NotFound, AttributeError):
                    replied = None
            add(replied)

        try:
            recent_count = 0
            async for message in ctx.history(limit=6):
                if current is not None and message.id == current.id:
                    continue
                add(message)
                recent_count += 1
                if recent_count >= 5:
                    break
        except (discord.HTTPException, discord.NotFound, AttributeError, TypeError):
            pass

        for message in messages:
            url = await self._message_url(ctx, message)
            if url:
                return url
        return None

    @commands.hybrid_command(
        name="download",
        aliases=("dl",),
        enabled=True,
        extras={"usage": "[url] [-format mp4 -title <name> -ignore_checks -hidden]"},
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        url="Optional media URL. If omitted, checks the reply and five recent messages.",
    )
    async def download(
        self,
        ctx: Context,
        url: str | None = commands.param(
            default=None,
            description="Optional supported media URL or use a recent message.",
        ),
        *,
        flags: DownloadFlags,
    ):
        """Download media from a supported website.

        -# -format    Choose MP4, MP3, WEBM, or GIF. Defaults to MP4.
        -# -title     Change the downloaded file name.
        -# -ignore_checks  Owner only. Allow temporary hosting above Discord's limit.
        -# -hidden    Hide the response when using the app command.
        """

        ignore_checks = bool(flags.ignore_checks)
        if ignore_checks and not await ctx.bot.is_owner(ctx.author):
            raise commands.BadArgument("Only the bot owner can use **-ignore_checks**.")

        if not url:
            url = await self._find_recent_url(ctx)
            if not url:
                raise commands.BadArgument(
                    "Provide a supported media URL, reply to one, or send one in "
                    "your five most recent messages."
                )

        if is_discord_media_url(url):
            return

        source_url = url
        async with ctx.typing(ephemeral=flags.hidden):
            try:
                url = await TenorUrlConverter().convert(ctx, source_url)
                img = cast(BytesIO, await to_image(ctx.session, url))
                downloader = Downloader(
                    ctx,
                    url,
                    allow_temporary_hosting=True,
                    ignore_checks=ignore_checks,
                )
                if img.getbuffer().nbytes > downloader.max_filesize:
                    try:
                        hosted_url = await upload_temporary_media(
                            ctx.bot,
                            img.getvalue(),
                            "tenor.gif",
                            content_type="image/gif",
                            ignore_size_limit=ignore_checks,
                        )
                    except TemporaryMediaError:
                        await ctx.send(
                            f"This GIF exceeds {downloader.upload_limit_description} "
                            "and temporary hosting is unavailable.",
                            ephemeral=flags.hidden,
                        )
                        return
                    container = ui.Container(
                        ui.MediaGallery(MediaGalleryItem(hosted_url)),
                        ui.TextDisplay(
                            "-# Discord's upload limit was exceeded. "
                            "This link expires in 30 minutes."
                        ),
                        accent_color=ctx.bot.embedcolor,
                    )
                    view_type = type("HostedDownloadView", (ui.LayoutView,), {})
                    view = view_type(timeout=None)
                    view.add_item(container)
                    await ctx.send(view=view, ephemeral=flags.hidden)
                    await record_download(ctx, source_url)
                    return
                await ctx.send(
                    file=discord.File(img, filename="tenor.gif"), ephemeral=True
                )
                await record_download(ctx, source_url)

                return

            except commands.BadArgument:
                pass

            # Resolve Klipy pages to their direct media URL before yt-dlp sees
            # them. This avoids sending the Cloudflare-protected HTML page to
            # the generic extractor.
            try:
                url = await KlipyUrlConverter(flags.format).convert(ctx, url)
            except commands.BadArgument:
                pass

            dl = Downloader(
                ctx,
                url,
                format=flags.format,
                filename=flags.title,
                allow_temporary_hosting=True,
                ignore_checks=ignore_checks,
            )

            await dl.download()
