"""Community image and GIF submissions and the approved post library.

Posts intentionally live in their own tables and review channel instead of
sharing the video library.  That keeps media-type checks and user preferences
independent, and makes it impossible for a video upload to enter the image
library by accident.
"""

from __future__ import annotations

import asyncio
import mimetypes
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from utils.converters import KlipyUrlConverter, MediaConverter, TenorUrlConverter
from utils.downloads import (
    GIF_FILTER,
    Downloader,
    is_discord_media_url,
    is_downloadable_media_page,
    normalize_download_url,
)
from utils.errors import DownloadError
from utils.functions import get_or_fetch_user
from utils.network import (
    REDIRECT_STATUSES,
    canonical_media_url,
    refresh_discord_attachment_url,
    validate_connected_peer,
    validate_public_url,
)
from utils.paginator import LayoutPager
from utils.vars import base_header

from .library_uploads import post_media_condition, resolve_library_filters
from .upload_limits import consume_manual_uploads, manual_upload_lock
from .video import VideoCommands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


POST_REVIEW_CHANNEL_ID = 1538743622260752414
POST_REVIEW_GUILD_ID = 939497177821110272
POST_REVIEW_MODERATOR_ROLE_ID = 1538971877563703427
POST_LIBRARY_ID_LOCK = 0x504F5354  # "POST"
POST_MAX_BYTES = 500 * 1024 * 1024
POST_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
POST_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    # Some media converters return AVIF even when the source is an animated
    # GIF. Convert that still-image response to PNG before submitting it to
    # Discord, which does not render AVIF attachments consistently.
    "image/avif": ".png",
}
POST_TIMEOUT = aiohttp.ClientTimeout(total=300, connect=15, sock_read=60)
POST_REPAIR_ITEM_TIMEOUT = 30.0
GIF_CONVERTER_HOSTS = frozenset({"gifconvert.vxtwitter.com"})
# These hosts are public media CDNs.  Some aiohttp transports do not expose a
# peer address for them after the response headers arrive, so the bounded
# fetcher may accept missing peer metadata after it has already validated the
# hostname and resolved it to public addresses.  Keep this list narrow and
# host-specific rather than weakening the SSRF check for arbitrary URLs.
POST_TRUSTED_CDN_HOSTS = frozenset(
    {
        "static.klipy.com",
        "media.tenor.com",
        "media1.tenor.com",
        "c.tenor.com",
    }
)


def _safe_text(value: object, *, limit: int = 1_000) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ""))
    )[:limit]


def _safe_filename(value: object, *, fallback: str = "post.png") -> str:
    name = Path(unquote(str(value or ""))).name
    name = "".join(c for c in name if c.isalnum() or c in {".", "-", "_", " "})
    name = name.strip(" .")
    return (name or fallback)[:255]


def _random_filename(source: object) -> str:
    suffix = Path(str(source or "")).suffix.casefold()
    if suffix not in POST_EXTENSIONS:
        suffix = ".png"
    token = secrets.token_urlsafe(8).strip("-_") or secrets.token_hex(8)
    return f"{token}{suffix}"


def _discord_gif_filename(source: object) -> str:
    """Return a stable marker name for GIFs normalized for Discord."""

    stem = Path(str(source or "post")).stem
    if stem.casefold().endswith("-discord"):
        return _safe_filename(f"{stem}.gif", fallback="post-discord.gif")
    return _safe_filename(f"{stem}-discord.gif", fallback="post-discord.gif")


def _is_post_filename(filename: str) -> bool:
    return Path(filename).suffix.casefold() in POST_EXTENSIONS


class PostDenyModal(discord.ui.Modal, title="Deny post"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Explain why this image or GIF was denied.",
        required=False,
        max_length=1_000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, view: "PostReviewView") -> None:
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.view_ref.cog._can_review(interaction):
            await interaction.response.send_message(
                "Only Fishie reviewers can use these controls.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        reason = str(self.reason.value).strip() or "No reason was provided."
        result = await self.view_ref.cog._deny_post(
            interaction, self.view_ref.upload_id, reason, block=False
        )
        if result:
            await self.view_ref.mark_denied(
                f"❌ Post denied.\nReason: {_safe_text(reason)}"
            )
        await interaction.followup.send(
            (
                "The post was denied and the uploader was notified."
                if result
                else "That submission has already been reviewed."
            ),
            ephemeral=True,
        )
        self.view_ref.stop()


class PostReviewView(discord.ui.View):
    """Persistent moderation controls for one pending image/GIF submission."""

    def __init__(
        self,
        cog: "PostCommands",
        upload_id: int,
        *,
        block_disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.upload_id = upload_id
        self.review_message: discord.Message | None = None
        self.approve_button = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"fishie:post:{upload_id}:approve",
        )
        self.deny_button = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"fishie:post:{upload_id}:deny",
        )
        self.block_button = discord.ui.Button(
            label="Block User",
            style=discord.ButtonStyle.danger,
            custom_id=f"fishie:post:{upload_id}:block",
            disabled=block_disabled,
        )
        self.approve_button.callback = self.approve
        self.deny_button.callback = self.deny
        self.block_button.callback = self.block
        self.add_item(self.approve_button)
        self.add_item(self.deny_button)
        self.add_item(self.block_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog._can_review(interaction):
            return True
        await interaction.response.send_message(
            "Only Fishie reviewers can use these controls.", ephemeral=True
        )
        return False

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def mark_denied(self, text: str) -> None:
        """Leave the review record visible while disabling its controls."""

        self.disable_all()
        if self.review_message is None:
            return
        try:
            await self.review_message.edit(
                content=text,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            self.cog.bot.logger.warning(
                "Could not update denied post review message %s",
                self.upload_id,
                exc_info=True,
            )

    async def approve(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.review_message = interaction.message
        try:
            approved = await self.cog._approve_post(interaction, self.upload_id)
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        self.disable_all()
        if approved is not None:
            if interaction.message is not None:
                await interaction.message.edit(
                    content="✅ Post approved and added to the Fishie post library.",
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.followup.send(
                    "Post approved and added to the Fishie post library.",
                    ephemeral=True,
                )
        else:
            await interaction.followup.send(
                "That submission has already been reviewed.", ephemeral=True
            )
        self.stop()

    async def deny(self, interaction: discord.Interaction) -> None:
        self.review_message = interaction.message
        await interaction.response.send_modal(PostDenyModal(self))

    async def block(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.review_message = interaction.message
        try:
            denied = await self.cog._deny_post(
                interaction,
                self.upload_id,
                "The uploader has been blocked from submitting posts.",
                block=True,
            )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        if denied:
            await self.mark_denied(
                "🚫 Post denied. The uploader has been blocked from submitting posts."
            )
        else:
            await interaction.followup.send(
                "That submission has already been reviewed.", ephemeral=True
            )
        self.stop()


class PostUploadsPageSource:
    """Components V2 page source for approved image/GIF uploads."""

    def __init__(
        self,
        cog: "PostCommands",
        ctx: Context,
        rows: list[dict[str, Any]],
        *,
        title: str,
    ) -> None:
        self.cog = cog
        self.ctx = ctx
        self.rows = rows
        self.title = title

    def get_max_pages(self) -> int:
        return len(self.rows)

    async def prepare_page(self, page_number: int) -> bool:
        if page_number < 0 or page_number >= len(self.rows):
            return False
        row = self.rows[page_number]
        if row.get("current_url") is None:
            row["current_url"] = await self.cog._current_post_url(
                upload_id=int(row["id"]),
                source_url=str(row["source_url"]),
                review_message_id=(
                    int(row["review_message_id"])
                    if row.get("review_message_id") is not None
                    else None
                ),
            )
        if row.get("uploader_name") is None:
            try:
                user = await get_or_fetch_user(self.ctx.bot, int(row["uploader_id"]))
                row["uploader_name"] = getattr(user, "name", None)
            except (discord.HTTPException, discord.NotFound):
                row["uploader_name"] = None
        return True

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        if not self.rows:
            return [
                discord.ui.TextDisplay(f"## {self.title}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("No approved image or GIF uploads were found."),
            ]
        row = self.rows[page_number]
        upload_id = int(row["id"])
        library_id = int(row.get("library_id") or upload_id)
        uploader_id = int(row["uploader_id"])
        uploader_name = _safe_text(
            row.get("uploader_name") or f"User {uploader_id}", limit=100
        )
        source_url = str(row.get("current_url") or row["source_url"])
        metadata = discord.ui.TextDisplay(
            f"-# Library ID `{library_id}` · Uploaded by {uploader_name} (`{uploader_id}`)\n"
            f"Page {page_number + 1}/{len(self.rows)}"
        )
        items: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay(f"## {self.title}")]
        if source_url:
            items.extend(
                (
                    discord.ui.Separator(),
                    discord.ui.MediaGallery(discord.MediaGalleryItem(source_url)),
                    discord.ui.Separator(),
                    metadata,
                )
            )
        else:
            items.extend(
                (
                    discord.ui.Separator(),
                    discord.ui.TextDisplay("The post URL is currently unavailable."),
                    discord.ui.Separator(),
                    metadata,
                )
            )
        return items


class PostCommands:
    """Mixin used by the Fun cog for the community image/GIF library."""

    bot: Fishie

    async def register_post_views(self) -> None:
        registered: dict[int, PostReviewView] = getattr(self, "_post_review_views", {})
        self._post_review_views = registered
        rows = await self.bot.pool.fetch(
            "SELECT id, review_message_id, uploader_id FROM post_uploads "
            "WHERE status = 'pending' AND review_message_id IS NOT NULL"
        )
        for row in rows:
            try:
                upload_id = int(row["id"])
                protected = await self._uploader_has_protected_role(
                    int(row["uploader_id"])
                )
                view = PostReviewView(
                    self,
                    upload_id,
                    block_disabled=protected is not False,
                )
                self.bot.add_view(view, message_id=int(row["review_message_id"]))
                registered[upload_id] = view
            except (discord.HTTPException, ValueError):
                self.bot.logger.warning(
                    "Could not restore post review controls for upload %s",
                    row["id"],
                    exc_info=True,
                )

    def unregister_post_views(self) -> None:
        registered: dict[int, PostReviewView] = getattr(self, "_post_review_views", {})
        remove_view = getattr(
            getattr(self.bot, "_connection", None), "remove_view", None
        )
        if remove_view is not None:
            for view in registered.values():
                remove_view(view)
        registered.clear()

    async def _post_review_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(POST_REVIEW_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(POST_REVIEW_CHANNEL_ID)
            except (discord.HTTPException, discord.NotFound):
                return None
        if not isinstance(channel, discord.TextChannel):
            return None
        return channel

    @staticmethod
    def _message_discord_media_url(message: object) -> str | None:
        """Get the Discord URL for a file attached to a review message.

        Discord can return an empty ``attachments`` array for a message whose
        attachment-backed embed has already been resolved.  In that case the
        CDN URL remains in the embed image object.  Only Discord media hosts
        are accepted, so an arbitrary external embed can never become a
        library source.
        """

        for attachment in getattr(message, "attachments", ()) or ():
            url = str(getattr(attachment, "url", "") or "")
            if url and is_discord_media_url(url):
                return url
        for embed in getattr(message, "embeds", ()) or ():
            image = getattr(embed, "image", None)
            for attribute in ("url", "proxy_url"):
                url = str(getattr(image, attribute, "") or "")
                if url and is_discord_media_url(url):
                    return url
        return None

    async def _can_review(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == int(self.bot.config["ids"]["owner_id"]):
            return True
        guild = interaction.guild
        if guild is None:
            return False
        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        if (
            member is not None
            and guild.id == POST_REVIEW_GUILD_ID
            and any(
                getattr(role, "id", None) == POST_REVIEW_MODERATOR_ROLE_ID
                for role in getattr(member, "roles", ())
            )
        ):
            return True
        return bool(member and member.guild_permissions.manage_guild)

    async def _uploader_has_protected_role(self, user_id: int) -> bool | None:
        """Return whether a post uploader has the protected uploader role."""

        guild = self.bot.get_guild(POST_REVIEW_GUILD_ID)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(POST_REVIEW_GUILD_ID)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return False
            except (discord.Forbidden, discord.HTTPException):
                return None
        return any(
            getattr(role, "id", None) == POST_REVIEW_MODERATOR_ROLE_ID
            for role in getattr(member, "roles", ())
        )

    def _is_bot_owner(self, user_id: int) -> bool:
        return user_id == int(self.bot.config["ids"]["owner_id"])

    async def _post_counts(self, user_id: int) -> tuple[int, int]:
        row = await self.bot.pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status = 'approved') AS approved, "
            "COUNT(*) FILTER (WHERE status = 'denied') AS denied "
            "FROM post_uploads WHERE uploader_id = $1",
            user_id,
        )
        return (int(row["approved"] or 0), int(row["denied"] or 0)) if row else (0, 0)

    @staticmethod
    def _is_post_attachment(attachment: discord.Attachment) -> bool:
        content_type = (attachment.content_type or "").casefold().split(";", 1)[0]
        suffix = Path(attachment.filename).suffix.casefold()
        return suffix in POST_EXTENSIONS and (
            not content_type or content_type in POST_CONTENT_TYPES
        )

    @staticmethod
    def _post_filename(url: str, content_type: str) -> str:
        filename = _safe_filename(
            Path(unquote(urlsplit(url).path)).name, fallback="post"
        )
        if Path(filename).suffix.casefold() in POST_EXTENSIONS:
            return filename
        extension = mimetypes.guess_extension(content_type.casefold().split(";", 1)[0])
        if content_type.casefold().split(";", 1)[0] == "image/avif":
            extension = ".png"
        if extension is None:
            extension = POST_CONTENT_TYPES.get(content_type.casefold(), ".png")
        if extension.casefold() not in POST_EXTENSIONS:
            extension = ".png"
        return _safe_filename(f"{Path(filename).stem}{extension}")

    @staticmethod
    def _looks_like_post_data(header: bytes, _filename: str | None = None) -> bool:
        """Check common image signatures when a CDN omits its MIME type."""

        if header.startswith((b"GIF87a", b"GIF89a", b"\x89PNG\r\n\x1a\n")):
            return True
        if header.startswith(b"\xff\xd8\xff"):
            return True
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return True
        if (
            len(header) >= 12
            and header[4:8] == b"ftyp"
            and header[8:12]
            in {
                b"avif",
                b"avis",
            }
        ):
            return True
        return False

    @staticmethod
    def _convert_avif_to_png(path: Path) -> tuple[Path, int]:
        """Convert a direct AVIF response to a Discord-compatible PNG."""

        output = path.with_suffix(".png")
        with Image.open(path) as image:
            image.load()
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")
            image.save(output, format="PNG", optimize=True)
        path.unlink(missing_ok=True)
        return output, output.stat().st_size

    @staticmethod
    def _gif_converter_source(url: str) -> str | None:
        """Return the animated source from a public GIF-converter URL."""

        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host not in GIF_CONVERTER_HOSTS:
            return None
        source = parse_qs(parsed.query).get("url", [None])[0]
        if not isinstance(source, str):
            return None
        source = unquote(source).strip()
        if not source.startswith(("https://", "http://")):
            return None
        return source

    async def _download_gif_converter_source(
        self, ctx: Context, source_url: str, original_source_url: str
    ) -> tuple[Path, str, int, str]:
        """Download a converter's source video and encode it as a GIF.

        ``gifconvert.vxtwitter.com/convert.avif`` returns an animated AVIF
        image sequence. Discord displays that response as a still image, so
        use the source URL embedded in its query string instead and create a
        normal GIF before submitting it to the post library.
        """

        video_path, _, _, _ = await VideoCommands._download_video_url(
            cast(Any, self), source_url
        )
        gif_path = video_path.with_suffix(".gif")
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-vf",
                GIF_FILTER,
                "-loop",
                "0",
                str(gif_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=POST_TIMEOUT.total or 300
            )
        except asyncio.TimeoutError as error:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            gif_path.unlink(missing_ok=True)
            video_path.unlink(missing_ok=True)
            raise commands.BadArgument(
                "That video took too long to convert to a GIF."
            ) from error
        except (FileNotFoundError, OSError) as error:
            gif_path.unlink(missing_ok=True)
            video_path.unlink(missing_ok=True)
            raise commands.BadArgument(
                "The video could not be converted to a GIF."
            ) from error

        video_path.unlink(missing_ok=True)
        if process.returncode != 0 or not gif_path.is_file():
            gif_path.unlink(missing_ok=True)
            detail = stderr.decode(errors="replace").strip() if stderr else ""
            raise commands.BadArgument(
                "The video could not be converted to a GIF."
                if not detail
                else f"The video could not be converted to a GIF: {detail[-300:]}"
            )
        size = gif_path.stat().st_size
        if size > POST_MAX_BYTES:
            gif_path.unlink(missing_ok=True)
            raise commands.BadArgument("Images and GIFs must be 500 MB or smaller.")
        return gif_path, "post.gif", size, original_source_url

    async def _normalize_gif_for_discord(self, path: Path) -> tuple[Path, int]:
        """Re-encode a GIF with complete frames that Discord can decode.

        Some GIF encoders write partial-frame deltas or unusual disposal
        metadata. Desktop image viewers can reconstruct those files, while
        Discord's media proxy may show ``Image failed to load``. Rebuilding
        the palette and disabling delta-frame encoding gives Discord a normal
        animated GIF without changing the post embed itself.
        """

        output = path.with_name(f"{path.stem}-discord.gif")
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-vf",
                GIF_FILTER,
                "-gifflags",
                "-offsetting-transdiff",
                "-loop",
                "0",
                str(output),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=POST_TIMEOUT.total or 300
            )
        except asyncio.TimeoutError as error:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            output.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise commands.BadArgument(
                "That GIF took too long to prepare for Discord."
            ) from error
        except (FileNotFoundError, OSError) as error:
            output.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise commands.BadArgument(
                "That GIF could not be prepared for Discord."
            ) from error

        path.unlink(missing_ok=True)
        if process.returncode != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            detail = stderr.decode(errors="replace").strip() if stderr else ""
            raise commands.BadArgument(
                "That GIF could not be prepared for Discord."
                if not detail
                else f"That GIF could not be prepared for Discord: {detail[-300:]}"
            )
        size = output.stat().st_size
        if size > POST_MAX_BYTES:
            output.unlink(missing_ok=True)
            raise commands.BadArgument("Images and GIFs must be 500 MB or smaller.")
        return output, size

    async def _download_post_url(
        self, url: str, *, normalize_gif: bool = True
    ) -> tuple[Path, str, int, str]:
        current = normalize_download_url(url.strip().strip("<>"))
        current = await refresh_discord_attachment_url(self.bot, current)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="fishie-post-", suffix=".upload", delete=False
            ) as handle:
                temporary = Path(handle.name)
            for _ in range(6):
                await validate_public_url(current)
                async with self.bot.session.get(
                    current,
                    allow_redirects=False,
                    timeout=POST_TIMEOUT,
                    headers=base_header,
                ) as response:
                    current_host = (
                        (urlsplit(current).hostname or "").casefold().rstrip(".")
                    )
                    validate_connected_peer(
                        response,
                        allow_missing_peer=current_host in POST_TRUSTED_CDN_HOSTS,
                    )
                    if response.status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise commands.BadArgument(
                                "The post URL returned an invalid redirect."
                            )
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise commands.BadArgument(
                            f"The post server returned HTTP {response.status}."
                        )
                    content_type = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .casefold()
                    )
                    is_avif = (
                        content_type == "image/avif"
                        or Path(urlsplit(str(response.url)).path).suffix.casefold()
                        == ".avif"
                    )
                    filename = self._post_filename(str(response.url), content_type)
                    generic_content_type = content_type in {
                        "",
                        "application/octet-stream",
                        "binary/octet-stream",
                    }
                    if not _is_post_filename(filename) or (
                        content_type
                        and content_type not in POST_CONTENT_TYPES
                        and not generic_content_type
                    ):
                        raise commands.BadArgument(
                            "That URL did not return a supported image or GIF."
                        )
                    if (
                        response.content_length is not None
                        and response.content_length > POST_MAX_BYTES
                    ):
                        raise commands.BadArgument(
                            "Images and GIFs must be 500 MB or smaller."
                        )
                    size = 0
                    with temporary.open("wb") as output:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > POST_MAX_BYTES:
                                raise commands.BadArgument(
                                    "Images and GIFs must be 500 MB or smaller."
                                )
                            output.write(chunk)
                    if size == 0:
                        raise commands.BadArgument(
                            "That post URL returned an empty file."
                        )
                    with temporary.open("rb") as source:
                        header = source.read(16)
                    if not self._looks_like_post_data(header):
                        raise commands.BadArgument(
                            "That URL did not return a supported image or GIF."
                        )
                    if is_avif:
                        try:
                            temporary, size = await asyncio.to_thread(
                                self._convert_avif_to_png, temporary
                            )
                            filename = _safe_filename(
                                f"{Path(filename).stem}.png", fallback="post.png"
                            )
                        except (OSError, ValueError) as error:
                            raise commands.BadArgument(
                                "That image format could not be converted for Discord."
                            ) from error
                    elif normalize_gif and filename.casefold().endswith(".gif"):
                        temporary, size = await self._normalize_gif_for_discord(
                            temporary
                        )
                    return temporary, filename, size, str(response.url)
            raise commands.BadArgument("The post URL redirected too many times.")
        except commands.CommandError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except asyncio.CancelledError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise commands.BadArgument("The post URL could not be fetched.") from error

    async def _download_post_with_downloader(
        self,
        ctx: Context,
        source_url: str,
        original_source_url: str,
        *,
        normalize_gif: bool = True,
    ) -> tuple[Path, str, int, str]:
        """Use the shared guarded downloader for direct and page media.

        This is deliberately shared with the regular download command.  In
        particular, direct Tenor CDN URLs must not fall through to yt-dlp,
        which has no page extractor for ``media1.tenor.com``.
        """

        try:
            data, filename = await Downloader(
                ctx, source_url, format="gif", hidden=True
            ).download_for_processing(max_bytes=POST_MAX_BYTES, timeout=300)
        except DownloadError as error:
            raise commands.BadArgument(str(error)) from error
        if not data:
            raise commands.BadArgument("That post URL returned an empty file.")
        safe_filename = _safe_filename(filename, fallback="post.gif")
        if not _is_post_filename(safe_filename):
            raise commands.BadArgument(
                "That page returned a video. Post uploads only accept images and GIFs."
            )
        try:
            with tempfile.NamedTemporaryFile(
                prefix="fishie-post-downloaded-",
                suffix=Path(safe_filename).suffix,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
        except OSError as error:
            raise commands.BadArgument(
                "The downloaded post could not be saved."
            ) from error
        if normalize_gif and safe_filename.casefold().endswith(".gif"):
            temporary, size = await self._normalize_gif_for_discord(temporary)
        else:
            size = len(data)
        return temporary, safe_filename, size, original_source_url

    async def _download_tenor_source(
        self,
        ctx: Context,
        source_url: str,
        original_source_url: str,
        *,
        normalize_gif: bool,
    ) -> tuple[Path, str, int, str]:
        """Download a Tenor source with a direct-fetch fallback.

        Tenor's CDN is a normal direct media endpoint, but it has returned
        different transport metadata to aiohttp over time.  Try the shared
        downloader first, then the post fetcher.  If both fail, retain both
        concrete errors so the owner command never reports an unexplained
        generic failure.
        """

        errors: list[str] = []
        for candidate in TenorUrlConverter.media_url_variants(source_url):
            try:
                return await self._download_post_with_downloader(
                    ctx,
                    candidate,
                    original_source_url,
                    normalize_gif=normalize_gif,
                )
            except commands.BadArgument as downloader_error:
                errors.append(f"{candidate}: {downloader_error}")
                self.bot.logger.warning(
                    "Shared downloader could not fetch Tenor source %s: %s",
                    candidate,
                    downloader_error,
                )
                try:
                    return await self._download_post_url(
                        candidate, normalize_gif=normalize_gif
                    )
                except commands.BadArgument as direct_error:
                    errors.append(f"{candidate} direct fetch: {direct_error}")

        detail = " | ".join(errors)
        raise commands.BadArgument(
            f"Tenor download failed for every known media URL variant: {detail[:1_500]}"
        )

    async def _download_post_source(
        self, ctx: Context, url: str, *, normalize_gif: bool = True
    ) -> tuple[Path, str, int, str]:
        source_url = normalize_download_url(url.strip().strip("<>"))
        original_source_url = source_url
        parsed_source = urlsplit(source_url)
        source_host = (parsed_source.hostname or "").casefold().rstrip(".")

        converter_source = self._gif_converter_source(source_url)
        if converter_source is not None:
            return await self._download_gif_converter_source(
                ctx, converter_source, original_source_url
            )

        # Tenor page URLs are not reliable yt-dlp inputs for a post upload.
        # Resolve the page through the same converter used by media commands,
        # then fetch the original GIF through the bounded direct-media path.
        # This also avoids accidentally accepting the MP4 preview that Discord
        # sometimes exposes when a Tenor link is copied on mobile.
        if source_host in {"tenor.com", "www.tenor.com", "tenor.co"}:
            resolved_url = await TenorUrlConverter().convert(ctx, source_url)
            return await self._download_tenor_source(
                ctx,
                resolved_url,
                original_source_url,
                normalize_gif=normalize_gif,
            )

        # A direct Tenor CDN URL is already the original GIF.  Send it through
        # the shared downloader so it uses its direct-media path instead of
        # attempting to run yt-dlp against a CDN file URL.
        if source_host in TenorUrlConverter._MEDIA_HOSTS:
            return await self._download_tenor_source(
                ctx,
                source_url,
                original_source_url,
                normalize_gif=normalize_gif,
            )

        # Klipy's page converter resolves page links to ``static.klipy.com``
        # media URLs.  Those URLs are often MP4 files even though the original
        # item is a GIF.  Send them through Downloader so its Klipy-specific
        # conversion produces a real GIF before we enforce the post media
        # extension check.  Treating the resolved MP4 as a direct post URL
        # would reject it as an unsupported video.
        is_klipy_media = (
            parsed_source.scheme.casefold() == "https"
            and source_host == "static.klipy.com"
        )
        if source_host in {"klipy.com", "www.klipy.com"}:
            # Resolve the page explicitly before handing it to Downloader.
            # yt-dlp's generic Klipy extractor is intermittently challenged,
            # while the small API response remains stable and gives us the
            # exact static media URL needed for GIF conversion.
            source_url = await KlipyUrlConverter("gif").convert(ctx, source_url)
            is_klipy_media = True
        if not is_downloadable_media_page(source_url) and not is_klipy_media:
            return await self._download_post_url(
                source_url, normalize_gif=normalize_gif
            )
        temporary, safe_filename, size, _ = await self._download_post_with_downloader(
            ctx,
            source_url,
            original_source_url,
            normalize_gif=normalize_gif,
        )
        # Keep the user-facing source (the original page when a resolver was
        # used) in the database rather than leaking the CDN implementation
        # detail. Direct static URLs remain unchanged.
        return temporary, safe_filename, size, original_source_url

    async def _reply_post_message(self, ctx: Context) -> discord.Message | None:
        """Fetch the message explicitly replied to, without scanning history."""

        current = getattr(ctx, "message", None)
        reference = getattr(current, "reference", None)
        message_id = getattr(reference, "message_id", None)
        if message_id is None:
            return None
        replied = getattr(reference, "resolved", None)
        if not isinstance(replied, discord.Message):
            try:
                replied = await ctx.fetch_message(message_id)
            except (discord.HTTPException, discord.NotFound, AttributeError):
                return None
        return replied

    async def _reply_post_sources(
        self, ctx: Context, replied: discord.Message | None = None
    ) -> tuple[list[discord.Attachment], list[str]]:
        if replied is None:
            replied = await self._reply_post_message(ctx)
        if replied is None:
            return [], []
        attachments = [
            cast(discord.Attachment, candidate)
            for candidate in MediaConverter._message_attachments(replied)
            if self._is_post_attachment(cast(discord.Attachment, candidate))
        ]
        # Discord exposes an uploaded attachment through both
        # ``message.attachments`` and ``_message_media_url``.  Treat the
        # attachment as the canonical source so it cannot be submitted twice.
        if attachments:
            return attachments, []

        try:
            content_url = await MediaConverter()._message_content_media_url(
                ctx, replied
            )
        except commands.BadArgument:
            content_url = None
        if content_url:
            # Prefer the URL written in the message. Discord may also expose
            # a proxied embed URL for the same media, which would otherwise
            # create a duplicate review submission.
            return [], [normalize_download_url(content_url)]
        embedded_url = MediaConverter._message_media_url(replied)
        if embedded_url:
            return [], [normalize_download_url(embedded_url)]
        return [], []

    @staticmethod
    def _is_library_response(message: discord.Message, kind: str = "post") -> bool:
        """Recognize a Fishie library response, including Components V2 text."""

        marker = f"fishie {kind}"
        values = [str(getattr(message, "content", "") or "")]
        for embed in getattr(message, "embeds", ()):
            values.extend(
                str(getattr(embed, attribute, "") or "")
                for attribute in ("title", "description")
            )
            footer = getattr(embed, "footer", None)
            values.append(str(getattr(footer, "text", "") or ""))
        for component in getattr(message, "components", ()):
            try:
                values.append(str(component.to_dict()))
            except (AttributeError, TypeError):
                values.append(str(component))
        return marker in " ".join(values).casefold()

    async def _approved_source_exists(self, source_url: str) -> bool:
        canonical = canonical_media_url(source_url)
        rows = await self.bot.pool.fetch(
            "SELECT source_url FROM post_uploads "
            "WHERE status = 'approved' AND source_url IS NOT NULL"
        )
        return any(
            canonical_media_url(str(row["source_url"])) == canonical for row in rows
        )

    async def _dm_uploader(self, user_id: int, text: str) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(text, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            self.bot.logger.info("Could not DM post uploader %s", user_id)

    async def _dm_approved_post(
        self, user_id: int, upload_id: int, source_url: str
    ) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            view = discord.ui.LayoutView(timeout=300)
            view.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("## Your post was approved"),
                    discord.ui.Separator(),
                    discord.ui.MediaGallery(discord.MediaGalleryItem(source_url)),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"Library ID `{upload_id}`"),
                    accent_color=self.bot.embedcolor,
                )
            )
            await user.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            self.bot.logger.info(
                "Could not DM approved post %s to uploader %s", upload_id, user_id
            )

    async def _delete_review_message(self, message_id: int | None) -> bool:
        if not message_id:
            return True
        channel = await self._post_review_channel()
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException):
            self.bot.logger.warning(
                "Could not delete post review message %s", message_id, exc_info=True
            )
            return False
        return True

    async def _repair_review_gif(
        self,
        upload_id: int,
        message: discord.Message,
        attachment: discord.Attachment,
    ) -> discord.Attachment | None:
        """Replace an older GIF review attachment with a Discord-safe copy."""

        input_path: Path | None = None
        output_path: Path | None = None
        review_file: discord.File | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="fishie-post-repair-", suffix=".gif", delete=False
            ) as handle:
                input_path = Path(handle.name)
            await attachment.save(input_path, use_cached=False)
            output_path, _ = await self._normalize_gif_for_discord(input_path)
            filename = _discord_gif_filename(attachment.filename)
            review_file = discord.File(output_path, filename=filename)

            embeds = [
                discord.Embed.from_dict(embed.to_dict())
                for embed in getattr(message, "embeds", ())
            ]
            if embeds:
                embeds[0].set_image(url=f"attachment://{filename}")
            edit_kwargs: dict[str, Any] = {"attachments": [review_file]}
            if embeds:
                edit_kwargs["embeds"] = embeds
            edited = await message.edit(**edit_kwargs)
            updated_attachments = getattr(edited, "attachments", ()) or getattr(
                message, "attachments", ()
            )
            if not updated_attachments:
                return None
            try:
                await self.bot.pool.execute(
                    "UPDATE post_uploads SET filename = $2 WHERE id = $1",
                    upload_id,
                    filename,
                )
            except Exception:
                self.bot.logger.warning(
                    "Could not persist repaired filename for post %s",
                    upload_id,
                    exc_info=True,
                )
            return cast(discord.Attachment, updated_attachments[0])
        except (commands.BadArgument, discord.HTTPException, OSError):
            self.bot.logger.warning(
                "Could not repair GIF attachment for post %s",
                upload_id,
                exc_info=True,
            )
            return None
        finally:
            if review_file is not None:
                review_file.close()
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            if input_path is not None:
                input_path.unlink(missing_ok=True)

    async def _review_attachment_url(
        self, upload_id: int, message_id: int
    ) -> str | None:
        """Return the current CDN URL for a post's review attachment.

        The URL saved when a submission is created can be the original source
        URL (for example a Tenor URL) or an expired Discord CDN URL.  The
        review message is the durable source of truth because Discord gives us
        a fresh attachment URL whenever it is fetched.
        """

        channel = await self._post_review_channel()
        if channel is None:
            self.bot.logger.warning(
                "Could not refresh attachment URL for post %s: review channel unavailable",
                upload_id,
            )
            return None
        try:
            message = await channel.fetch_message(message_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            self.bot.logger.warning(
                "Could not refresh attachment URL for post %s from review message %s",
                upload_id,
                message_id,
                exc_info=True,
            )
            return None
        attachment_url = self._message_discord_media_url(message)
        if not attachment_url:
            self.bot.logger.warning(
                "Post %s review message %s has no attachment",
                upload_id,
                message_id,
            )
            return None
        attachment = next(
            (
                candidate
                for candidate in (getattr(message, "attachments", ()) or ())
                if str(getattr(candidate, "url", "") or "") == attachment_url
            ),
            None,
        )
        if attachment is None:
            # The URL came from the resolved embed image.  There is no
            # Attachment object to normalize, but the URL is still the file
            # Discord received and is safe to persist.
            return attachment_url
        attachment_name = str(getattr(attachment, "filename", ""))
        if Path(attachment_name).suffix.casefold() == ".gif" and not Path(
            attachment_name
        ).stem.casefold().endswith("-discord"):
            repaired = await self._repair_review_gif(upload_id, message, attachment)
            if repaired is not None:
                attachment = repaired
                attachment_url = str(getattr(attachment, "url", "") or "")
        return attachment_url

    async def _current_post_url(
        self, *, upload_id: int, source_url: str, review_message_id: int | None
    ) -> str:
        if review_message_id is None:
            return source_url if is_discord_media_url(source_url) else ""
        current_url = await self._review_attachment_url(upload_id, review_message_id)
        if not current_url:
            return source_url if is_discord_media_url(source_url) else ""
        if current_url != source_url:
            try:
                await self.bot.pool.execute(
                    "UPDATE post_uploads SET source_url = $2 WHERE id = $1",
                    upload_id,
                    current_url,
                )
            except Exception:
                # A stale database URL must never prevent a valid review
                # attachment from being sent to the user.
                self.bot.logger.warning(
                    "Could not persist refreshed attachment URL for post %s",
                    upload_id,
                    exc_info=True,
                )
        return current_url

    async def _repair_post_review_message(
        self,
        ctx: Context,
        row: Any,
        message: discord.Message,
        review_channel: discord.TextChannel,
    ) -> str:
        """Make one legacy review message use a Discord file attachment.

        Older post submissions sometimes kept the original page URL in the
        review message instead of uploading the media to the review channel.
        The review message is the durable source of truth, so this helper
        downloads that media, replaces the embed image with an attachment,
        and stores the fresh Discord CDN URL in the database.
        """

        upload_id = int(row["id"])
        attachments = list(getattr(message, "attachments", ()) or ())
        if attachments:
            attachment = cast(discord.Attachment, attachments[0])
            filename = str(getattr(attachment, "filename", ""))
            if Path(filename).suffix.casefold() == ".gif" and not Path(
                filename
            ).stem.casefold().endswith("-discord"):
                repaired = await self._repair_review_gif(upload_id, message, attachment)
                if repaired is None:
                    self._post_repair_failure_details[upload_id] = (
                        "the existing GIF attachment could not be re-encoded"
                    )
                    return "failed"
                attachment = repaired
                filename = str(getattr(attachment, "filename", filename))
            attachment_url = str(getattr(attachment, "url", "") or "")
            if not attachment_url:
                return "failed"
            if filename:
                await self.bot.pool.execute(
                    "UPDATE post_uploads SET source_url = $2, filename = $3 "
                    "WHERE id = $1",
                    upload_id,
                    attachment_url,
                    filename,
                )
            else:
                await self.bot.pool.execute(
                    "UPDATE post_uploads SET source_url = $2 WHERE id = $1",
                    upload_id,
                    attachment_url,
                )
            return "repaired"

        candidates: list[str] = []
        seen: set[str] = set()

        def add_candidate(value: object) -> None:
            if not isinstance(value, str) or not value.strip():
                return
            value = normalize_download_url(value.strip().strip("<>"))
            key = canonical_media_url(value)
            if key not in seen:
                seen.add(key)
                candidates.append(value)

        add_candidate(row["source_url"])
        for value in MediaConverter._message_media_urls(message):
            add_candidate(value)
        try:
            content_url = await MediaConverter()._message_content_media_url(
                ctx, message
            )
        except commands.BadArgument:
            content_url = None
        add_candidate(content_url)
        if not candidates:
            self._post_repair_failure_details[upload_id] = (
                "the review message and database did not contain a media URL"
            )
            return "missing_source"

        last_failure = ""
        for candidate in candidates:
            temporary_path: Path | None = None
            review_file: discord.File | None = None
            try:
                (
                    temporary_path,
                    source_filename,
                    media_size,
                    _,
                ) = await self._download_post_source(
                    ctx, candidate, normalize_gif=False
                )
                filename = _random_filename(source_filename)
                if filename.casefold().endswith(".gif"):
                    filename = _discord_gif_filename(filename)
                review_limit = int(
                    getattr(review_channel.guild, "filesize_limit", 0) or 0
                )
                if review_limit and media_size > review_limit:
                    last_failure = (
                        f"downloaded file is {media_size / 1024 / 1024:.1f} MB, "
                        f"review channel limit is {review_limit / 1024 / 1024:.1f} MB"
                    )
                    continue

                review_file = discord.File(temporary_path, filename=filename)
                embeds: list[discord.Embed] = []
                try:
                    embeds = [
                        discord.Embed.from_dict(embed.to_dict())
                        for embed in getattr(message, "embeds", ())
                    ]
                except (TypeError, ValueError):
                    self.bot.logger.warning(
                        "Could not copy embeds while repairing post %s",
                        upload_id,
                        exc_info=True,
                    )
                if embeds:
                    embeds[0].set_image(url=f"attachment://{filename}")
                edit_kwargs: dict[str, Any] = {"attachments": [review_file]}
                if embeds:
                    edit_kwargs["embeds"] = embeds
                edited = await message.edit(**edit_kwargs)
                updated_attachments = getattr(edited, "attachments", ()) or getattr(
                    message, "attachments", ()
                )
                if not updated_attachments:
                    last_failure = "Discord returned no attachment after editing the review message"
                    continue
                updated_attachment = cast(discord.Attachment, updated_attachments[0])
                updated_url = str(getattr(updated_attachment, "url", "") or "")
                if not updated_url:
                    last_failure = "Discord returned an attachment without a URL"
                    continue
                await self.bot.pool.execute(
                    "UPDATE post_uploads SET source_url = $2, filename = $3 WHERE id = $1",
                    upload_id,
                    updated_url,
                    filename,
                )
                return "repaired"
            except (commands.CommandError, discord.HTTPException, OSError) as error:
                last_failure = f"{type(error).__name__}: {error}"
                self.bot.logger.warning(
                    "Could not repair post %s from %s",
                    upload_id,
                    candidate,
                    exc_info=True,
                )
            finally:
                if review_file is not None:
                    review_file.close()
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        self._post_repair_failure_details[upload_id] = (
            last_failure or "all media candidates failed without a reported reason"
        )
        return "failed"

    async def _repair_post_review_messages(self, ctx: Context) -> None:
        """Reconcile legacy external URLs with review-channel attachments."""

        channel = await self._post_review_channel()
        if channel is None:
            raise commands.BadArgument("The Fishie post review channel is unavailable.")
        rows = await self.bot.pool.fetch(
            "SELECT id, source_url, review_message_id FROM post_uploads "
            "WHERE status = 'approved' AND review_message_id IS NOT NULL ORDER BY id"
        )
        counts = {
            "repaired": 0,
            "already_discord": 0,
            "missing_message": 0,
            "missing_source": 0,
            "failed": 0,
        }
        failed_ids: list[str] = []
        self._post_repair_failure_details: dict[int, str] = {}
        for row in rows:
            upload_id = int(row["id"])
            if is_discord_media_url(str(row["source_url"] or "")):
                counts["already_discord"] += 1
                continue
            try:
                message = await channel.fetch_message(int(row["review_message_id"]))
            except discord.NotFound:
                counts["missing_message"] += 1
                self._post_repair_failure_details[upload_id] = (
                    "the stored review message no longer exists"
                )
                continue
            except (discord.Forbidden, discord.HTTPException):
                counts["failed"] += 1
                failed_ids.append(str(upload_id))
                self._post_repair_failure_details[upload_id] = (
                    "Fishie could not fetch the stored review message"
                )
                continue
            try:
                result = await asyncio.wait_for(
                    self._repair_post_review_message(ctx, row, message, channel),
                    timeout=POST_REPAIR_ITEM_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self.bot.logger.warning("Timed out repairing post %s", upload_id)
                self._post_repair_failure_details[upload_id] = (
                    f"repair exceeded the {POST_REPAIR_ITEM_TIMEOUT:g}-second timeout"
                )
                result = "failed"
            except Exception as error:
                self.bot.logger.warning(
                    "Could not finish repairing post %s",
                    upload_id,
                    exc_info=True,
                )
                self._post_repair_failure_details[upload_id] = (
                    f"{type(error).__name__}: {error}"
                )
                result = "failed"
            counts[result] += 1
            if result in {"failed", "missing_source"} and len(failed_ids) < 20:
                failed_ids.append(str(upload_id))

        summary = (
            f"Reconciled **{counts['repaired']:,}** post URL(s).\n"
            f"Already using Discord URLs: **{counts['already_discord']:,}**\n"
            f"Missing review messages: **{counts['missing_message']:,}**\n"
            f"Missing sources: **{counts['missing_source']:,}**\n"
            f"Failed: **{counts['failed']:,}**"
        )
        if failed_ids:
            summary += f"\nFailed/missing IDs: `{', '.join(failed_ids)}`"
            detail_lines = [
                f"`{upload_id}`: {discord.utils.escape_markdown(reason)[:240]}"
                for upload_id, reason in self._post_repair_failure_details.items()
                if str(upload_id) in failed_ids
            ]
            if detail_lines:
                summary += "\nFailure details:\n" + "\n".join(detail_lines[:12])
        await ctx.send(summary, allowed_mentions=discord.AllowedMentions.none())

    async def _approve_post(
        self, interaction: discord.Interaction, upload_id: int
    ) -> int | None:
        # The attachment on the review message is the copy that Fishie saved
        # for its library.  A submitted URL is deliberately never used here.
        # Pending rows have a NULL source_url until this exact attachment has
        # been found and approval is committed.
        review_message = getattr(interaction, "message", None)
        review_message_id = getattr(review_message, "id", None)
        review_attachment_url: str | None = None
        if review_message is not None:
            review_attachment_url = self._message_discord_media_url(review_message)

        # Component interactions normally include the message attachments,
        # but fetching the durable review message also handles older cached
        # interaction payloads and restarts safely.
        pending = await self.bot.pool.fetchrow(
            "SELECT review_message_id FROM post_uploads "
            "WHERE id = $1 AND status = 'pending'",
            upload_id,
        )
        if pending is None:
            return None
        stored_review_message_id = pending["review_message_id"]
        if stored_review_message_id is not None:
            review_message_id = int(stored_review_message_id)
        if not review_attachment_url and review_message_id is not None:
            channel = await self._post_review_channel()
            if channel is not None:
                try:
                    fetched = await channel.fetch_message(review_message_id)
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    fetched = None
                if fetched is not None:
                    review_attachment_url = self._message_discord_media_url(fetched)
        if not review_attachment_url or not is_discord_media_url(review_attachment_url):
            raise commands.BadArgument(
                "Fishie could not find the Discord attachment on that review "
                "message, so it was not approved."
            )
        async with self.bot.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1)", POST_LIBRARY_ID_LOCK
                )
                row = await connection.fetchrow(
                    """
                    WITH next_id AS (
                        SELECT candidate AS library_id
                        FROM generate_series(
                            1::BIGINT,
                            GREATEST(
                                1::BIGINT,
                                COALESCE(
                                    GREATEST(
                                        COALESCE((SELECT MAX(library_id) FROM post_uploads), 0),
                                        COALESCE((SELECT MAX(library_id) FROM post_library_deleted_ids), 0)
                                    ),
                                    0
                                ) + 1
                            )
                        ) AS series(candidate)
                        WHERE NOT EXISTS (
                            SELECT 1 FROM post_uploads
                            WHERE library_id = candidate
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM post_library_deleted_ids
                            WHERE library_id = candidate
                        )
                        ORDER BY candidate
                        LIMIT 1
                    )
                    UPDATE post_uploads AS upload
                    SET source_url = $3::TEXT,
                        review_message_id = COALESCE(upload.review_message_id, $4::BIGINT),
                        status = 'approved',
                        library_id = next_id.library_id,
                        approved_by = $2,
                        reviewed_at = now()
                    FROM next_id
                    WHERE upload.id = $1
                      AND upload.status = 'pending'
                      AND upload.library_id IS NULL
                    RETURNING upload.uploader_id, upload.source_url,
                              upload.library_id, upload.review_message_id
                    """,
                    upload_id,
                    interaction.user.id,
                    review_attachment_url,
                    review_message_id,
                )
        if row is None:
            return None
        library_id = int(row["library_id"])
        source_url = await self._current_post_url(
            upload_id=upload_id,
            source_url=str(row["source_url"]),
            review_message_id=(
                int(row["review_message_id"])
                if row["review_message_id"] is not None
                else None
            ),
        )
        await self._dm_approved_post(int(row["uploader_id"]), library_id, source_url)
        return library_id

    async def _deny_post(
        self,
        interaction: discord.Interaction,
        upload_id: int,
        reason: str,
        *,
        block: bool,
    ) -> bool:
        if block:
            pending = await self.bot.pool.fetchrow(
                "SELECT uploader_id FROM post_uploads "
                "WHERE id = $1 AND status = 'pending'",
                upload_id,
            )
            if pending is not None:
                protected = await self._uploader_has_protected_role(
                    int(pending["uploader_id"])
                )
                if protected is not False:
                    raise commands.BadArgument(
                        "This uploader has the protected uploader role and cannot be blocked."
                    )
        row = await self.bot.pool.fetchrow(
            "UPDATE post_uploads SET status = 'denied', denied_by = $2, denial_reason = $3, reviewed_at = now() "
            "WHERE id = $1 AND status = 'pending' RETURNING uploader_id, review_message_id",
            upload_id,
            interaction.user.id,
            reason[:1_000],
        )
        if row is None:
            return False
        uploader_id = int(row["uploader_id"])
        if block:
            await self.bot.pool.execute(
                "INSERT INTO post_upload_blocks (user_id, blocked_by) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET blocked_by = EXCLUDED.blocked_by, blocked_at = now()",
                uploader_id,
                interaction.user.id,
            )
            text = "Your post was denied and you have been blocked from submitting posts to Fishie."
        else:
            text = f"Your post was denied.\nReason: {_safe_text(reason)}"
        await self._dm_uploader(uploader_id, text)
        return True

    async def _post_alias_row(self, user_id: int | None, identifier: str) -> Any | None:
        if user_id is None:
            return None
        return await self.bot.pool.fetchrow(
            "SELECT p.id, p.library_id, p.source_url, p.filename, p.review_message_id FROM post_aliases a "
            "JOIN post_uploads p ON p.id = a.post_id WHERE a.user_id = $1 "
            "AND lower(btrim(a.alias)) = lower(btrim($2)) AND p.status = 'approved' "
            "AND p.library_id IS NOT NULL",
            user_id,
            identifier,
        )

    @staticmethod
    def _post_pool_filter(user_id: int | None) -> tuple[str, tuple[int, ...]]:
        if user_id is None:
            return "", ()
        return (
            " AND NOT EXISTS (SELECT 1 FROM post_library_blocks b WHERE b.user_id = $1 "
            "AND b.blocked_uploader_id = p.uploader_id) AND NOT EXISTS ("
            "SELECT 1 FROM post_library_hides h WHERE h.user_id = $1 AND h.post_id = p.id)",
            (user_id,),
        )

    async def _send_post(self, ctx: Context, identifier: str | None = None) -> None:
        requester_id = getattr(getattr(ctx, "author", None), "id", None)
        identifier = identifier.strip() if identifier is not None else None
        explicit = identifier is not None
        post_id: int | None = None
        row = (
            await self._post_alias_row(requester_id, identifier) if identifier else None
        )
        if row is None and identifier is not None:
            try:
                post_id = int(identifier)
            except ValueError as error:
                raise commands.BadArgument(
                    "That post ID or personal alias was not found."
                ) from error
            if post_id < 1:
                raise commands.BadArgument("The post ID must be positive.")
        if row is None and post_id is None:
            pool_filter, filter_args = self._post_pool_filter(requester_id)
            count_query = (
                "SELECT COUNT(*) FROM post_uploads p WHERE p.status = 'approved' "
                "AND p.library_id IS NOT NULL" + pool_filter
            )
            count = int(await self.bot.pool.fetchval(count_query, *filter_args) or 0)
            if count:
                offset = secrets.randbelow(count)
                if requester_id is None:
                    row = await self.bot.pool.fetchrow(
                        "SELECT p.id, p.library_id, p.source_url, p.filename, p.review_message_id FROM post_uploads p "
                        "WHERE p.status = 'approved' AND p.library_id IS NOT NULL "
                        "ORDER BY p.library_id OFFSET $1 LIMIT 1",
                        offset,
                    )
                else:
                    row = await self.bot.pool.fetchrow(
                        "SELECT p.id, p.library_id, p.source_url, p.filename, p.review_message_id FROM post_uploads p "
                        "WHERE p.status = 'approved' AND p.library_id IS NOT NULL"
                        + pool_filter
                        + " ORDER BY p.library_id OFFSET $2 LIMIT 1",
                        requester_id,
                        offset,
                    )
        elif row is None:
            row = await self.bot.pool.fetchrow(
                "SELECT id, library_id, source_url, filename, review_message_id FROM post_uploads "
                "WHERE status = 'approved' AND library_id = $1",
                post_id,
            )
        if row is None:
            await ctx.send(
                (
                    "That post does not exist or has not been approved."
                    if explicit
                    else "There are no approved posts available in your pool."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        source_url = await self._current_post_url(
            upload_id=int(row["id"]),
            source_url=str(row["source_url"]),
            review_message_id=(
                int(row["review_message_id"])
                if row["review_message_id"] is not None
                else None
            ),
        )
        if not source_url:
            await ctx.send(
                "That post has no usable Discord attachment yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Fishie post" if explicit else "## Random Fishie post"
                ),
                discord.ui.Separator(),
                discord.ui.MediaGallery(discord.MediaGalleryItem(source_url)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "Upload your own image or GIF with `fish post upload` · "
                    f"Library ID `{int(row.get('library_id') or row['id'])}`"
                ),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def _send_post_uploads(self, ctx: Context, target: str | None = None) -> None:
        filters = await resolve_library_filters(ctx, target)
        condition = post_media_condition(filters.media, alias="p")
        query = (
            "SELECT p.id, p.library_id, p.source_url, p.filename, "
            "p.review_message_id, p.uploader_id FROM post_uploads AS p "
            "WHERE p.status = 'approved' AND p.library_id IS NOT NULL"
            f"{condition}"
        )
        if filters.all_users:
            query += " ORDER BY p.library_id"
            rows = await self.bot.pool.fetch(query)
            title = "All Fishie post uploads"
        else:
            query += " AND p.uploader_id = $1 ORDER BY p.library_id"
            rows = await self.bot.pool.fetch(query, filters.user_id)
            title = (
                "Your Fishie post uploads"
                if filters.user_id == ctx.author.id
                else "Fishie post uploads"
            )
        source = PostUploadsPageSource(
            self, ctx, [dict(row) for row in rows], title=title
        )
        await LayoutPager(
            source, ctx=ctx, accent_color=self.bot.embedcolor, timeout=600
        ).start()

    async def _send_post_stats(self, ctx: Context) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT uploader_id, COUNT(*) AS total FROM post_uploads "
            "WHERE status = 'approved' AND library_id IS NOT NULL "
            "GROUP BY uploader_id ORDER BY total DESC, uploader_id ASC LIMIT 10"
        )
        lines: list[str] = []
        for row in rows:
            user_id = int(row["uploader_id"])
            try:
                user = await get_or_fetch_user(self.bot, user_id)
                username = user.name
            except (discord.HTTPException, discord.NotFound):
                username = f"User {user_id}"
            lines.append(
                f"**{_safe_text(username, limit=100)}** (`{user_id}`) · {int(row['total']):,} post(s)"
            )
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Fishie post upload stats"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "\n".join(lines) if lines else "No approved posts yet."
                ),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @cast(Any, commands.hybrid_group)(name="post", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        identifier="An approved post library ID or one of your aliases."
    )
    async def post(self, ctx: Context, *, identifier: str | None = None) -> None:
        """Show a random approved Fishie image/GIF, ID, or personal alias."""
        async with ctx.typing():
            await self._send_post(ctx, identifier)

    @post.command(name="random")
    @app_commands.describe(
        identifier="An optional approved post library ID or personal alias."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_random(self, ctx: Context, identifier: str | None = None) -> None:
        """Show a random approved Fishie image/GIF or a library ID."""
        async with ctx.typing():
            await self._send_post(ctx, identifier)

    @post.command(name="uploads")
    @app_commands.describe(
        target="A media filter, Discord user, `all`, or any combination."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_uploads(self, ctx: Context, *, target: str | None = None) -> None:
        """Browse approved image/GIF uploads for a user or the full library."""
        async with ctx.typing():
            await self._send_post_uploads(ctx, target)

    @cast(Any, commands.command)(name="posts")
    async def posts(self, ctx: Context, *, target: str | None = None) -> None:
        """Browse approved image/GIF uploads with optional media and user filters."""
        async with ctx.typing():
            await self._send_post_uploads(ctx, target)

    @post.command(name="repair", hidden=True)
    @commands.is_owner()
    async def post_repair(self, ctx: Context) -> None:
        """Repair legacy review messages that still use external URLs."""

        async with ctx.typing():
            await self._repair_post_review_messages(ctx)

    @post.command(name="stats")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_stats(self, ctx: Context) -> None:
        """Show the users with the most approved Fishie post uploads."""
        async with ctx.typing():
            await self._send_post_stats(ctx)

    @post.command(name="alias")
    @app_commands.describe(
        identifier="The approved post library ID.",
        alias="Your personal name for the post.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_alias(self, ctx: Context, identifier: int, *, alias: str) -> None:
        """Give an approved post a personal name for random library access."""
        alias_text = " ".join(alias.split()).strip()
        if not alias_text or len(alias_text) > 100:
            raise commands.BadArgument("Your post alias must be 1 to 100 characters.")
        row = await self.bot.pool.fetchrow(
            "SELECT id, library_id FROM post_uploads "
            "WHERE library_id = $1 AND status = 'approved'",
            identifier,
        )
        if row is None:
            await ctx.send(
                "That post does not exist or has not been approved.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        conflict = await self.bot.pool.fetchrow(
            "SELECT post_id FROM post_aliases WHERE user_id = $1 AND lower(btrim(alias)) = lower(btrim($2))",
            ctx.author.id,
            alias_text,
        )
        if conflict is not None and int(conflict["post_id"]) != int(row["id"]):
            await ctx.send(
                "You already use that alias for another post.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO post_aliases (post_id, user_id, alias) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, post_id) DO UPDATE SET alias = EXCLUDED.alias, created_at = now()",
            int(row["id"]),
            ctx.author.id,
            alias_text,
        )
        await ctx.send(
            f"Post `{identifier}` is now available as `{_safe_text(alias_text)}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @post.command(name="block")
    @app_commands.describe(
        user="Uploader whose posts should be hidden from your random pool."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_block(self, ctx: Context, user: discord.User) -> None:
        """Hide another uploader's posts from your random library results."""
        if user.id == ctx.author.id:
            await ctx.send(
                "You cannot block your own posts.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO post_library_blocks (user_id, blocked_uploader_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.author.id,
            user.id,
        )
        await ctx.send(
            f"Posts uploaded by `{_safe_text(user.name)}` are now hidden from your random pool.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @post.command(name="unblock")
    @app_commands.describe(
        user="Uploader whose posts should be shown in your random pool again."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_unblock(self, ctx: Context, user: discord.User) -> None:
        """Show an uploader's posts in your random library again."""
        result = await self.bot.pool.execute(
            "DELETE FROM post_library_blocks WHERE user_id = $1 AND blocked_uploader_id = $2",
            ctx.author.id,
            user.id,
        )
        await ctx.send(
            (
                f"Posts uploaded by `{_safe_text(user.name)}` are no longer hidden."
                if result.endswith(" 1")
                else "That uploader was not blocked in your post pool."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @post.command(name="hide")
    @app_commands.describe(
        identifier="Approved post library ID to hide from your random pool."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_hide(self, ctx: Context, identifier: int) -> None:
        """Hide one approved post from your random library results."""
        row = await self.bot.pool.fetchrow(
            "SELECT id FROM post_uploads WHERE library_id = $1 AND status = 'approved'",
            identifier,
        )
        if row is None:
            await ctx.send(
                "That post does not exist or has not been approved.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO post_library_hides (user_id, post_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.author.id,
            int(row["id"]),
        )
        await ctx.send(
            f"Post `{identifier}` is now hidden from your random pool.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @post.command(name="unhide")
    @app_commands.describe(
        identifier="Approved post library ID to show in your random pool again."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_unhide(self, ctx: Context, identifier: int) -> None:
        """Show a hidden post in your random library results again."""
        result = await self.bot.pool.execute(
            "DELETE FROM post_library_hides AS hidden USING post_uploads AS upload "
            "WHERE hidden.user_id = $1 AND hidden.post_id = upload.id "
            "AND upload.library_id = $2",
            ctx.author.id,
            identifier,
        )
        await ctx.send(
            (
                f"Post `{identifier}` is no longer hidden."
                if result.endswith(" 1")
                else "That post was not hidden in your pool."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @post.command(name="upload")
    @app_commands.describe(
        media="A public image/GIF file or supported download-page URL to submit for review.",
        attachment="An image or GIF attachment to submit for review.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_upload(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Submit one or more image/GIF attachments, a URL, or a replied-to post."""
        lock = manual_upload_lock(ctx)
        if lock.locked():
            await ctx.send(
                "Another upload is already being processed for you. Please wait for it to finish.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with lock:
            async with ctx.typing():
                await self._post_upload_impl(ctx, media, attachment)

    async def _submit_post(
        self,
        ctx: Context,
        *,
        media_url: str | None = None,
        attachment: discord.Attachment | None = None,
        allowed_media: set[str] | None = None,
    ) -> bool:
        temporary_path: Path | None = None
        upload_path: Path | None = None
        if media_url is not None:
            (
                temporary_path,
                filename,
                media_size,
                source_url,
            ) = await self._download_post_source(ctx, media_url)
            filename = _random_filename(filename)
            upload_path = temporary_path
        elif attachment is not None:
            if not self._is_post_attachment(attachment):
                raise commands.BadArgument(
                    "That attachment is not a supported image or GIF."
                )
            if attachment.size > POST_MAX_BYTES:
                raise commands.BadArgument("Images and GIFs must be 500 MB or smaller.")
            filename = _random_filename(attachment.filename)
            media_size = attachment.size
            source_url = attachment.url
            if Path(attachment.filename).suffix.casefold() == ".gif":
                try:
                    with tempfile.NamedTemporaryFile(
                        prefix="fishie-post-upload-",
                        suffix=".gif",
                        delete=False,
                    ) as handle:
                        temporary_path = Path(handle.name)
                    await attachment.save(temporary_path, use_cached=False)
                    temporary_path, media_size = await self._normalize_gif_for_discord(
                        temporary_path
                    )
                    upload_path = temporary_path
                except (OSError, discord.HTTPException) as error:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
                    raise commands.BadArgument(
                        "That GIF could not be prepared for Discord."
                    ) from error
        else:
            raise commands.BadArgument("No image or GIF was supplied.")

        if filename.casefold().endswith(".gif"):
            filename = _discord_gif_filename(filename)

        if allowed_media is not None:
            media_type = "gifs" if filename.casefold().endswith(".gif") else "images"
            if media_type not in allowed_media:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                return False

        submitted_source_url = source_url
        if await self._approved_source_exists(submitted_source_url):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise commands.BadArgument(
                "That image or GIF is already in the Fishie post library."
            )

        target = await self._post_review_channel()
        if target is None:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise commands.BadArgument(
                "The Fishie post review channel is unavailable right now."
            )
        review_limit = int(getattr(target.guild, "filesize_limit", 0) or 0)
        if review_limit and media_size > review_limit:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            limit_mb = review_limit / (1024 * 1024)
            raise commands.BadArgument(
                f"That post is too large ({limit_mb:g} MB upload limit)."
            )

        source_guild = ctx.guild
        source_text = (
            f"{_safe_text(source_guild.name)} (`{source_guild.id}`)"
            if source_guild is not None
            else "Direct message"
        )
        approved, denied = await self._post_counts(ctx.author.id)
        review_file: discord.File | None = None
        try:
            row = await self.bot.pool.fetchrow(
                "INSERT INTO post_uploads (source_url, filename, uploader_id, source_guild_id, source_channel_id) "
                "VALUES (NULL, $1, $2, $3, $4) RETURNING id",
                filename,
                ctx.author.id,
                source_guild.id if source_guild is not None else None,
                getattr(ctx.channel, "id", None),
            )
            if row is None:
                raise RuntimeError("Could not create the pending post submission.")
            upload_id = int(row["id"])
            protected = await self._uploader_has_protected_role(ctx.author.id)
            review_view = PostReviewView(
                self,
                upload_id,
                block_disabled=protected is not False,
            )
            embed = discord.Embed(
                title="Post submission",
                description=(
                    f"**Uploader:** {_safe_text(ctx.author.name)} (`{ctx.author.id}`)\n"
                    f"**Source guild:** {source_text}\n"
                    f"**Approved:** {approved:,} · **Denied:** {denied:,}\n"
                    f"**Submission ID:** `{upload_id}`"
                ),
                color=self.bot.embedcolor,
            )
            embed.set_footer(text="Pending review")
            if upload_path is not None:
                review_file = discord.File(upload_path, filename=filename)
                embed.set_image(url=f"attachment://{filename}")
            else:
                assert attachment is not None
                review_file = await attachment.to_file(use_cached=False, spoiler=False)
                embed.set_image(url=f"attachment://{review_file.filename}")
            review_message = await target.send(
                embed=embed,
                file=review_file,
                view=review_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if not getattr(review_message, "attachments", ()):
                # Discord may omit attachment metadata from the response to
                # send() even though the file was accepted.  Keep the review
                # message and resolve its attachment when it is approved.
                self.bot.logger.warning(
                    "Post review message %s returned without attachment metadata; "
                    "deferring attachment lookup until approval",
                    review_message.id,
                )
            await self.bot.pool.execute(
                "UPDATE post_uploads SET review_message_id = $2 WHERE id = $1",
                upload_id,
                review_message.id,
            )
        except Exception:
            if "upload_id" in locals():
                await self.bot.pool.execute(
                    "DELETE FROM post_uploads WHERE id = $1", upload_id
                )
            raise
        finally:
            if review_file is not None:
                review_file.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return True

    async def _post_upload_impl(
        self,
        ctx: Context,
        media: str | None = None,
        attachment: discord.Attachment | None = None,
        *,
        _allow_message_attachments: bool = True,
        allowed_media: set[str] | None = None,
        _enforce_rate_limit: bool = True,
    ) -> None:
        blocked = await self.bot.pool.fetchval(
            "SELECT 1 FROM post_upload_blocks WHERE user_id = $1", ctx.author.id
        )
        if blocked:
            await ctx.send(
                "You are blocked from submitting posts to Fishie.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        media_urls = (
            tuple(
                normalize_download_url(candidate)
                for candidate in MediaConverter._message_urls(media)
            )
            if media
            else ()
        )
        if media and not media_urls:
            media_urls = (normalize_download_url(media.strip()),)
        if len(media_urls) > 10:
            await ctx.send(
                "You can submit at most 10 URLs at a time.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        message = getattr(ctx, "message", None)
        message_attachments = (
            tuple(
                candidate
                for candidate in getattr(message, "attachments", ())
                if isinstance(candidate, discord.Attachment)
            )
            if _allow_message_attachments
            else ()
        )
        if media_urls and (attachment is not None or message_attachments):
            await ctx.send(
                "Provide either a media URL or an attachment, not both.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        attachments: list[discord.Attachment] = []
        urls: list[str] = []
        if media_urls:
            urls = list(media_urls)
        else:
            for candidate in message_attachments:
                if self._is_post_attachment(candidate):
                    attachments.append(candidate)
            if attachment is not None and attachment not in attachments:
                attachments.append(attachment)
            if not attachments:
                replied = await self._reply_post_message(ctx)
                if replied is not None and self._is_library_response(replied):
                    await ctx.send(
                        "no",
                        delete_after=3,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                reply_attachments, reply_urls = await self._reply_post_sources(
                    ctx, replied
                )
                attachments.extend(reply_attachments)
                urls.extend(reply_urls)
        if not attachments and not urls:
            await ctx.send(
                "Provide an image/GIF URL, attach one or more images/GIFs, or reply to a post containing them.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if _enforce_rate_limit:
            retry_after = consume_manual_uploads(ctx, len(attachments) + len(urls))
            if retry_after:
                await ctx.send(
                    f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        succeeded = 0
        failures: list[str] = []
        for candidate in attachments:
            try:
                submitted = await self._submit_post(
                    ctx,
                    attachment=candidate,
                    allowed_media=allowed_media,
                )
            except commands.BadArgument as error:
                failures.append(str(error))
            else:
                succeeded += int(submitted)
        for url in urls:
            try:
                submitted = await self._submit_post(
                    ctx,
                    media_url=url,
                    allowed_media=allowed_media,
                )
            except commands.BadArgument as error:
                failures.append(str(error))
            else:
                succeeded += int(submitted)
        if allowed_media is not None and not succeeded and not failures:
            # Automatic channels silently ignore media types disabled in
            # their server settings.  Manual ``post upload`` retains its
            # normal explanatory response because it does not pass a filter.
            return
        if succeeded:
            response = f"{succeeded} post{'s' if succeeded != 1 else ''} sent for review. I will notify you by DM when approved or denied."
            if failures:
                response += "\n" + "\n".join(
                    f"Skipped: {failure}" for failure in failures[:3]
                )
        else:
            response = failures[0] if failures else "No valid image or GIF was found."
        await ctx.send(response, allowed_mentions=discord.AllowedMentions.none())

    @post.command(name="delete")
    @app_commands.describe(
        identifier="Your approved library ID or `all`; the bot owner can clear the full library."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def post_delete(self, ctx: Context, *, identifier: str) -> None:
        """Delete an approved post you uploaded or clear your own uploads."""
        is_owner = self._is_bot_owner(ctx.author.id)
        if identifier.casefold() == "all":
            query = (
                "SELECT id, library_id, review_message_id FROM post_uploads "
                "WHERE status = 'approved' AND library_id IS NOT NULL"
            )
            args: tuple[object, ...] = ()
            if not is_owner:
                query += " AND uploader_id = $1"
                args = (ctx.author.id,)
            rows = await self.bot.pool.fetch(query + " ORDER BY library_id", *args)
            if not rows:
                await ctx.send(
                    "There are no approved posts to delete.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if (
                await ctx.prompt(
                    f"This will permanently delete {len(rows):,} approved post(s). Continue?",
                    confirm_label="Delete",
                    cancel_label="Cancel",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                is None
            ):
                await ctx.send(
                    "Post deletion cancelled.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            ids: list[int] = []
            for row in rows:
                if await self._delete_review_message(row["review_message_id"]):
                    ids.append(int(row["id"]))
            query = "DELETE FROM post_uploads WHERE id = ANY($1::BIGINT[]) AND status = 'approved'"
            args = (ids,)
            if not is_owner:
                query += " AND uploader_id = $2"
                args += (ctx.author.id,)
            if ids:
                await self.bot.pool.execute(
                    "INSERT INTO post_library_deleted_ids (library_id) "
                    "SELECT library_id FROM post_uploads "
                    "WHERE id = ANY($1::BIGINT[]) AND status = 'approved' "
                    "AND library_id IS NOT NULL ON CONFLICT DO NOTHING",
                    ids,
                )
                await self.bot.pool.execute(query, *args)
            await ctx.send(
                f"Deleted {len(ids):,} approved post(s).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            post_id = int(identifier)
        except ValueError as error:
            alias_row = await self._post_alias_row(ctx.author.id, identifier)
            if alias_row is None:
                raise commands.BadArgument(
                    "Provide a library ID, personal alias, or `all`."
                ) from error
            post_id = int(alias_row["id"])
            library_id = int(alias_row["library_id"])
            alias_label = _safe_text(identifier.strip(), limit=100)
            if (
                await ctx.prompt(
                    f"You are removing the alias `{alias_label}` for library post `{library_id}`. "
                    "This only removes the alias. If you own the post and want to delete the upload, "
                    f"run `fish post delete {library_id}` instead. Continue?",
                    confirm_label="Delete alias",
                    cancel_label="Cancel",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                is None
            ):
                await ctx.send(
                    "Alias deletion cancelled.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            result = await self.bot.pool.execute(
                "DELETE FROM post_aliases WHERE user_id = $1 AND post_id = $2",
                ctx.author.id,
                post_id,
            )
            await ctx.send(
                (
                    f"Removed alias `{alias_label}` for library post `{library_id}`. The uploaded post was not deleted."
                    if result.endswith(" 1")
                    else "That post alias no longer exists."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        row = await self.bot.pool.fetchrow(
            "SELECT id, library_id, uploader_id, review_message_id FROM post_uploads "
            "WHERE library_id = $1 AND status = 'approved'",
            post_id,
        )
        if row is None:
            await ctx.send(
                "No approved post had that ID.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not is_owner and int(row["uploader_id"]) != ctx.author.id:
            await ctx.send(
                "You can only delete posts that you uploaded.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if (
            await ctx.prompt(
                f"Permanently delete approved post `{int(row['library_id'])}`?",
                confirm_label="Delete",
                cancel_label="Cancel",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            is None
        ):
            await ctx.send(
                "Post deletion cancelled.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._delete_review_message(row["review_message_id"]):
            await ctx.send(
                "That post's review message could not be deleted, so the library entry was kept. Try again later.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO post_library_deleted_ids (library_id) VALUES ($1) "
            "ON CONFLICT DO NOTHING",
            int(row["library_id"]),
        )
        query = "DELETE FROM post_uploads WHERE id = $1 AND status = 'approved'"
        args = (int(row["id"]),)
        if not is_owner:
            query += " AND uploader_id = $2"
            args += (ctx.author.id,)
        result = await self.bot.pool.execute(query, *args)
        await ctx.send(
            (
                "Deleted that approved post."
                if result.endswith(" 1")
                else "No approved post had that ID."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
