"""Community video submissions and the approved video library."""

from __future__ import annotations

import asyncio
import mimetypes
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.converters import MediaConverter
from utils.downloads import (
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

from .library_uploads import resolve_library_filters, video_media_condition
from .upload_limits import consume_manual_uploads, manual_upload_lock

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


# These are deliberately kept as constants until the review channel is moved
# into the operator configuration.  The source guild is not a permission
# boundary.  Users may submit from a DM or another guild, but reviews always
# go to this private moderation channel.
VIDEO_REVIEW_GUILD_ID = 939497177821110272
VIDEO_REVIEW_CHANNEL_ID = 1536730398292181042
VIDEO_REVIEW_MODERATOR_ROLE_ID = 1538971877563703427
VIDEO_MAX_BYTES = 500 * 1024 * 1024
VIDEO_RESIZE_THRESHOLD = 15 * 1024 * 1024
VIDEO_EXTENSIONS = {".avi", ".m4v", ".mov", ".mp4", ".mkv", ".webm"}
GIF_SIGNATURES = (b"GIF87a", b"GIF89a")
VIDEO_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=300,
    connect=15,
    sock_read=60,
)
VIDEO_RESIZE_TIMEOUT = 60
VIDEO_LIBRARY_ID_LOCK = 0x564944454F  # "VIDEO"


def _safe_text(value: object, *, limit: int = 1_000) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ""))
    )[:limit]


def _safe_video_filename(value: object, *, fallback: str = "video.mp4") -> str:
    """Return a safe filename for a Discord upload."""

    name = Path(unquote(str(value or ""))).name
    name = "".join(
        character
        for character in name
        if character.isalnum() or character in {".", "-", "_", " "}
    ).strip(" .")
    return (name or fallback)[:255]


def _random_video_filename(source: object) -> str:
    """Create a non-identifying filename while preserving the media type."""

    suffix = Path(str(source or "")).suffix.casefold()
    if suffix not in VIDEO_EXTENSIONS:
        suffix = ".mp4"
    token = secrets.token_urlsafe(8).strip("-_") or secrets.token_hex(8)
    return f"{token}{suffix}"


class VideoDenyModal(discord.ui.Modal, title="Deny video"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Explain why this video was denied.",
        required=False,
        max_length=1_000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, view: "VideoReviewView") -> None:
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
        result = await self.view_ref.cog._deny_video(
            interaction,
            self.view_ref.upload_id,
            reason,
            block=False,
        )
        if result:
            await self.view_ref.mark_denied(
                f"❌ Video denied.\nReason: {_safe_text(reason)}"
            )
            await interaction.followup.send(
                "The video was denied and the uploader was notified.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "That submission has already been reviewed.", ephemeral=True
            )
        self.view_ref.stop()


class VideoReviewView(discord.ui.View):
    """Persistent moderation controls for one pending video submission."""

    def __init__(
        self,
        cog: "VideoCommands",
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
            custom_id=f"fishie:video:{upload_id}:approve",
        )
        self.deny_button = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"fishie:video:{upload_id}:deny",
        )
        self.block_button = discord.ui.Button(
            label="Block User",
            style=discord.ButtonStyle.danger,
            custom_id=f"fishie:video:{upload_id}:block",
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
                "Could not update denied video review message %s",
                self.upload_id,
                exc_info=True,
            )

    async def approve(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.review_message = interaction.message
        try:
            library_id = await self.cog._approve_video(interaction, self.upload_id)
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        self.disable_all()
        if library_id is not None:
            message = interaction.message
            if message is not None:
                await message.edit(
                    content="✅ Video approved and added to the Fishie video library.",
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.followup.send(
                    "Video approved and added to the Fishie video library.",
                    ephemeral=True,
                )
        else:
            await interaction.followup.send(
                "That submission has already been reviewed.", ephemeral=True
            )
        self.stop()

    async def deny(self, interaction: discord.Interaction) -> None:
        self.review_message = interaction.message
        await interaction.response.send_modal(VideoDenyModal(self))

    async def block(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.review_message = interaction.message
        try:
            blocked = await self.cog._deny_video(
                interaction,
                self.upload_id,
                "The uploader has been blocked from submitting videos.",
                block=True,
            )
        except commands.BadArgument as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        if blocked:
            await self.mark_denied(
                "🚫 Video denied. The uploader has been blocked from submitting videos."
            )
        else:
            await interaction.followup.send(
                "That submission has already been reviewed.", ephemeral=True
            )
        self.stop()


class VideoUploadsPageSource:
    """Components V2 page source for approved video uploads."""

    def __init__(
        self,
        cog: "VideoCommands",
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
            row["current_url"] = await self.cog._current_video_url(
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
                uploader = await get_or_fetch_user(
                    self.ctx.bot, int(row["uploader_id"])
                )
                row["uploader_name"] = getattr(uploader, "name", None)
            except (discord.HTTPException, discord.NotFound):
                row["uploader_name"] = None
        return True

    def format_page(self, page_number: int) -> list[discord.ui.Item[Any]]:
        if not self.rows:
            return [
                discord.ui.TextDisplay(f"## {self.title}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay("No approved video uploads were found."),
            ]

        row = self.rows[page_number]
        library_id = int(row.get("library_id") or row["id"])
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
                    discord.ui.TextDisplay("The video URL is currently unavailable."),
                    discord.ui.Separator(),
                    metadata,
                )
            )
        return items


class VideoCommands:
    """Mixin used by the Fun cog for the community video library."""

    bot: Fishie

    async def register_video_views(self) -> None:
        """Restore controls for pending review messages after a restart."""
        registered: dict[int, VideoReviewView] = getattr(
            self, "_video_review_views", {}
        )
        self._video_review_views = registered
        rows = await self.bot.pool.fetch(
            "SELECT id, review_message_id, uploader_id FROM video_uploads "
            "WHERE status = 'pending' AND review_message_id IS NOT NULL"
        )
        for row in rows:
            try:
                upload_id = int(row["id"])
                protected = await self._uploader_has_protected_role(
                    int(row["uploader_id"])
                )
                view = VideoReviewView(
                    self,
                    upload_id,
                    block_disabled=protected is not False,
                )
                self.bot.add_view(view, message_id=int(row["review_message_id"]))
                registered[upload_id] = view
            except (discord.HTTPException, ValueError):
                self.bot.logger.warning(
                    "Could not restore video review controls for upload %s",
                    row["id"],
                    exc_info=True,
                )

    def unregister_video_views(self) -> None:
        """Remove this cog's persistent views when the extension is reloaded."""
        registered: dict[int, VideoReviewView] = getattr(
            self, "_video_review_views", {}
        )
        remove_view = getattr(
            getattr(self.bot, "_connection", None), "remove_view", None
        )
        if remove_view is not None:
            for view in registered.values():
                remove_view(view)
        registered.clear()

    async def _video_review_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(VIDEO_REVIEW_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(VIDEO_REVIEW_CHANNEL_ID)
            except (discord.HTTPException, discord.NotFound):
                return None
        if not isinstance(channel, discord.TextChannel):
            return None
        if channel.guild.id != VIDEO_REVIEW_GUILD_ID:
            self.bot.logger.error(
                "Configured video review channel %s belongs to guild %s, expected %s",
                VIDEO_REVIEW_CHANNEL_ID,
                channel.guild.id,
                VIDEO_REVIEW_GUILD_ID,
            )
            return None
        return channel

    @staticmethod
    def _message_discord_media_url(message: object) -> str | None:
        """Get a Discord-hosted file URL from attachments or an embed image."""

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
        user = interaction.user
        if user.id == int(self.bot.config["ids"]["owner_id"]):
            return True
        guild = interaction.guild
        if guild is None:
            return False
        member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
        if member is None:
            return False
        if guild.id == VIDEO_REVIEW_GUILD_ID and any(
            getattr(role, "id", None) == VIDEO_REVIEW_MODERATOR_ROLE_ID
            for role in getattr(member, "roles", ())
        ):
            return True
        return bool(member.guild_permissions.manage_guild)

    async def _uploader_has_protected_role(self, user_id: int) -> bool | None:
        """Return whether a video uploader has the protected uploader role.

        ``None`` means the role could not be verified.  Review controls fail
        closed in that case so a temporary Discord/API failure cannot be used
        to block a protected uploader.
        """

        guild = self.bot.get_guild(VIDEO_REVIEW_GUILD_ID)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(VIDEO_REVIEW_GUILD_ID)
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
            getattr(role, "id", None) == VIDEO_REVIEW_MODERATOR_ROLE_ID
            for role in getattr(member, "roles", ())
        )

    def _is_bot_owner(self, user_id: int) -> bool:
        return user_id == int(self.bot.config["ids"]["owner_id"])

    async def _video_counts(self, user_id: int) -> tuple[int, int]:
        row = await self.bot.pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status = 'approved') AS approved, "
            "COUNT(*) FILTER (WHERE status = 'denied') AS denied "
            "FROM video_uploads WHERE uploader_id = $1",
            user_id,
        )
        return (int(row["approved"] or 0), int(row["denied"] or 0)) if row else (0, 0)

    @staticmethod
    def _is_video(attachment: discord.Attachment) -> bool:
        content_type = (attachment.content_type or "").casefold()
        suffix = Path(attachment.filename).suffix.casefold()
        if suffix == ".gif" or content_type.split(";", 1)[0].strip() == "image/gif":
            return False
        return content_type.startswith("video/") or suffix in VIDEO_EXTENSIONS

    @staticmethod
    def _is_video_response(content_type: str, filename: str) -> bool:
        """Accept video responses and common CDN responses for video files."""

        normalized = content_type.casefold().split(";", 1)[0].strip()
        suffix = Path(filename).suffix.casefold()
        if suffix == ".gif" or normalized == "image/gif":
            return False
        if normalized.startswith("video/"):
            return True
        # Some attachment/CDN endpoints use a generic content type.
        return suffix in VIDEO_EXTENSIONS and normalized in {
            "",
            "application/octet-stream",
            "binary/octet-stream",
        }

    @staticmethod
    def _is_gif_data(data: bytes) -> bool:
        """Detect GIF data even when a downloader gives it a video suffix."""

        return data[:6] in GIF_SIGNATURES

    @staticmethod
    def _video_filename(url: str, content_type: str) -> str:
        path_name = Path(unquote(urlsplit(url).path)).name
        filename = _safe_video_filename(path_name, fallback="video")
        if Path(filename).suffix.casefold() in VIDEO_EXTENSIONS:
            return filename

        extension = mimetypes.guess_extension(
            content_type.casefold().split(";", 1)[0].strip()
        )
        if extension is None or extension.casefold() not in VIDEO_EXTENSIONS:
            extension = ".mp4"
        return _safe_video_filename(f"{Path(filename).stem}{extension}")

    async def _download_video_url(
        self,
        url: str,
    ) -> tuple[Path, str, int, str]:
        """Download a public video URL to a bounded temporary file.

        A temporary file keeps a 500 MB submission from occupying that much
        resident memory.  Redirects are checked individually to prevent an
        otherwise safe-looking URL from being redirected to a private host.
        """

        current = normalize_download_url(url.strip().strip("<>"))
        current = await refresh_discord_attachment_url(self.bot, current)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="fishie-video-", suffix=".upload", delete=False
            ) as handle:
                temporary = Path(handle.name)

            for _ in range(6):
                await validate_public_url(current)
                async with self.bot.session.get(
                    current,
                    allow_redirects=False,
                    timeout=VIDEO_DOWNLOAD_TIMEOUT,
                ) as response:
                    validate_connected_peer(response)
                    if response.status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise commands.BadArgument(
                                "The video URL returned an invalid redirect."
                            )
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise commands.BadArgument(
                            f"The video server returned HTTP {response.status}."
                        )

                    content_type = response.headers.get("Content-Type", "")
                    filename = self._video_filename(str(response.url), content_type)
                    if not self._is_video_response(content_type, filename):
                        raise commands.BadArgument(
                            "That URL did not return a supported video file."
                        )

                    if (
                        response.content_length is not None
                        and response.content_length > VIDEO_MAX_BYTES
                    ):
                        raise commands.BadArgument("Videos must be 500 MB or smaller.")

                    size = 0
                    header = bytearray()
                    with temporary.open("wb") as output:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if len(header) < 6:
                                header.extend(chunk[: 6 - len(header)])
                                if len(header) == 6 and self._is_gif_data(
                                    bytes(header)
                                ):
                                    raise commands.BadArgument(
                                        "GIFs are not accepted by the video command."
                                    )
                            size += len(chunk)
                            if size > VIDEO_MAX_BYTES:
                                raise commands.BadArgument(
                                    "Videos must be 500 MB or smaller."
                                )
                            output.write(chunk)
                    if size == 0:
                        raise commands.BadArgument(
                            "That video URL returned an empty file."
                        )
                    return temporary, filename, size, str(response.url)

            raise commands.BadArgument("The video URL redirected too many times.")
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
            raise commands.BadArgument("The video URL could not be fetched.") from error

    async def _download_video_source(
        self,
        ctx: Context,
        url: str,
    ) -> tuple[Path, str, int, str]:
        """Fetch either a direct video file or a supported download page.

        Direct files use the bounded HTTP path above.  Supported post/page
        URLs use the same guarded Downloader workflow as ``fish download``
        and media effects, including redirect, host, timeout, and size
        checks.  Only the first media item is submitted for review.
        """

        source_url = normalize_download_url(url.strip().strip("<>"))
        if not is_downloadable_media_page(source_url):
            return await self._download_video_url(source_url)

        try:
            data, filename = await Downloader(
                ctx,
                source_url,
                format="mp4",
                hidden=True,
            ).download_for_processing(
                max_bytes=VIDEO_MAX_BYTES,
                timeout=VIDEO_DOWNLOAD_TIMEOUT.total or 300,
            )
        except DownloadError as error:
            raise commands.BadArgument(str(error)) from error

        if not data:
            raise commands.BadArgument("That video URL returned an empty file.")
        if self._is_gif_data(data):
            raise commands.BadArgument("GIFs are not accepted by the video command.")
        safe_filename = _safe_video_filename(filename, fallback="video.mp4")
        suffix = Path(safe_filename).suffix.casefold()
        if suffix not in VIDEO_EXTENSIONS:
            raise commands.BadArgument(
                "That URL did not return a supported video file."
            )
        try:
            with tempfile.NamedTemporaryFile(
                prefix="fishie-video-downloaded-",
                suffix=Path(safe_filename).suffix or ".mp4",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
        except OSError as error:
            raise commands.BadArgument(
                "The downloaded video could not be saved."
            ) from error
        return temporary, safe_filename, len(data), source_url

    async def _reply_message(self, ctx: Context) -> discord.Message | None:
        """Fetch the message explicitly replied to, without scanning history."""

        current = getattr(ctx, "message", None)
        reference = getattr(current, "reference", None)
        message_id = getattr(reference, "message_id", None)
        if message_id is None:
            return None

        replied = getattr(reference, "resolved", None)
        if isinstance(replied, discord.Message):
            return replied
        try:
            fetched = await ctx.fetch_message(message_id)
        except (discord.HTTPException, discord.NotFound, AttributeError):
            return None
        return fetched if isinstance(fetched, discord.Message) else None

    async def _reply_video_source(
        self, ctx: Context, replied: discord.Message | None = None
    ) -> tuple[discord.Attachment | None, str | None]:
        """Return the first video media from the explicitly replied-to message."""

        attachments, url = await self._reply_video_sources(ctx, replied)
        return (attachments[0] if attachments else None), url

    async def _reply_video_sources(
        self, ctx: Context, replied: discord.Message | None = None
    ) -> tuple[list[discord.Attachment], str | None]:
        """Return all video attachments from the explicitly replied-to message."""

        if replied is None:
            replied = await self._reply_message(ctx)
        if replied is None:
            return [], None

        attachments = [
            cast(discord.Attachment, candidate)
            for candidate in MediaConverter._message_attachments(replied)
            if self._is_video(cast(discord.Attachment, candidate))
        ]
        if attachments:
            return attachments, None

        # A reply can contain a direct URL or a supported post URL in its
        # content.  MediaConverter handles Tenor/Klipy pages and the same
        # allowlisted download-page detection used by media effects.  It does
        # not inspect recent messages when called with the message explicitly.
        try:
            content_url = await MediaConverter()._message_content_media_url(
                ctx, replied
            )
        except commands.BadArgument:
            content_url = None
        if content_url:
            return [], normalize_download_url(content_url)

        # Preserve URLs exposed through an embed, but let the bounded fetch
        # below verify that the response is actually a supported video.
        embedded_url = MediaConverter._message_media_url(replied)
        if embedded_url:
            return [], normalize_download_url(embedded_url)
        return [], None

    @staticmethod
    def _is_library_response(message: discord.Message, kind: str = "video") -> bool:
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
            "SELECT source_url FROM video_uploads "
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
            self.bot.logger.info("Could not DM video uploader %s", user_id)

    async def _dm_approved_video(
        self, user_id: int, upload_id: int, source_url: str
    ) -> None:
        """Tell the uploader which approved library entry is theirs."""

        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            view = discord.ui.LayoutView(timeout=300)
            view.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("## Your video was approved"),
                    discord.ui.Separator(),
                    discord.ui.MediaGallery(discord.MediaGalleryItem(source_url)),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"Library ID `{upload_id}`"),
                    accent_color=self.bot.embedcolor,
                )
            )
            await user.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            self.bot.logger.info(
                "Could not DM approved video %s to uploader %s", upload_id, user_id
            )

    async def _resize_video_to_scale(
        self,
        source: Path,
        *,
        gif: bool = False,
        scale_percent: int = 50,
    ) -> Path | None:
        """Encode a scaled copy for uploads just over the review limit."""

        suffix = ".gif" if gif else ".mp4"
        output: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="fishie-video-resized-", suffix=suffix, delete=False
            ) as handle:
                output = Path(handle.name)

            scale = max(50, min(90, scale_percent)) / 100
            scale_filter = (
                f"scale=trunc(iw*{scale:g}/2)*2:trunc(ih*{scale:g}/2)*2:flags=lanczos"
            )
            if suffix == ".gif":
                arguments = (
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-vf",
                    scale_filter,
                    "-an",
                    "-loop",
                    "0",
                    str(output),
                )
            else:
                arguments = (
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-vf",
                    scale_filter,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "30",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    "-movflags",
                    "+faststart",
                    str(output),
                )

            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=VIDEO_RESIZE_TIMEOUT
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise

            if process.returncode != 0 or output is None:
                self.bot.logger.warning(
                    "Could not resize video %s: %s",
                    source,
                    stderr.decode("utf-8", errors="replace")[-1_000:],
                )
                if output is not None:
                    output.unlink(missing_ok=True)
                return None

            if output.stat().st_size == 0:
                output.unlink(missing_ok=True)
                return None
            return output
        except (asyncio.TimeoutError, OSError) as error:
            self.bot.logger.warning("Could not resize video %s", source, exc_info=error)
            if output is not None:
                output.unlink(missing_ok=True)
            return None

    async def _delete_review_message(self, message_id: int | None) -> bool:
        """Delete a review message, treating an already-missing message as removed."""

        if not message_id:
            return True
        channel = await self._video_review_channel()
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException):
            self.bot.logger.warning(
                "Could not delete video review message %s", message_id, exc_info=True
            )
            return False
        return True

    async def _current_video_url(
        self,
        *,
        upload_id: int,
        source_url: str,
        review_message_id: int | None,
    ) -> str:
        """Resolve a fresh Discord attachment URL from the durable message ID."""

        if review_message_id is None:
            return source_url if is_discord_media_url(source_url) else ""
        channel = await self._video_review_channel()
        if channel is None:
            return source_url if is_discord_media_url(source_url) else ""
        try:
            message = await channel.fetch_message(review_message_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            self.bot.logger.warning(
                "Could not refresh attachment URL for approved video %s",
                upload_id,
                exc_info=True,
            )
            return source_url if is_discord_media_url(source_url) else ""
        current_url = self._message_discord_media_url(message)
        if not current_url:
            self.bot.logger.warning(
                "Approved video %s review message %s has no attachment",
                upload_id,
                review_message_id,
            )
            return source_url if is_discord_media_url(source_url) else ""
        if current_url != source_url:
            try:
                await self.bot.pool.execute(
                    "UPDATE video_uploads SET source_url = $2 WHERE id = $1",
                    upload_id,
                    current_url,
                )
            except Exception:
                self.bot.logger.warning(
                    "Could not persist refreshed attachment URL for video %s",
                    upload_id,
                    exc_info=True,
                )
        return current_url

    async def _approve_video(
        self, interaction: discord.Interaction, upload_id: int
    ) -> int | None:
        # Pending rows intentionally have no durable source URL.  Only the
        # attachment on the review message sent by Fishie may become a
        # library URL after approval.
        review_message = getattr(interaction, "message", None)
        review_message_id = getattr(review_message, "id", None)
        review_attachment_url: str | None = None
        if review_message is not None:
            review_attachment_url = self._message_discord_media_url(review_message)
        pending = await self.bot.pool.fetchrow(
            "SELECT review_message_id FROM video_uploads "
            "WHERE id = $1 AND status = 'pending'",
            upload_id,
        )
        if pending is None:
            return None
        stored_review_message_id = pending["review_message_id"]
        if stored_review_message_id is not None:
            review_message_id = int(stored_review_message_id)
        if not review_attachment_url and review_message_id is not None:
            channel = await self._video_review_channel()
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
                    "SELECT pg_advisory_xact_lock($1)", VIDEO_LIBRARY_ID_LOCK
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
                                        COALESCE((SELECT MAX(library_id) FROM video_uploads), 0),
                                        COALESCE((SELECT MAX(library_id) FROM video_library_deleted_ids), 0)
                                    ),
                                    0
                                ) + 1
                            )
                        ) AS series(candidate)
                        WHERE NOT EXISTS (
                            SELECT 1 FROM video_uploads
                            WHERE library_id = candidate
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM video_library_deleted_ids
                            WHERE library_id = candidate
                        )
                        ORDER BY candidate
                        LIMIT 1
                    )
                    UPDATE video_uploads AS upload
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
                    RETURNING upload.uploader_id, upload.source_url, upload.library_id
                    """,
                    upload_id,
                    interaction.user.id,
                    review_attachment_url,
                    review_message_id,
                )
        if row is None:
            return None
        library_id = int(row["library_id"])
        await self._dm_approved_video(
            int(row["uploader_id"]),
            library_id,
            str(row["source_url"]),
        )
        return library_id

    async def _deny_video(
        self,
        interaction: discord.Interaction,
        upload_id: int,
        reason: str,
        *,
        block: bool,
    ) -> bool:
        if block:
            pending = await self.bot.pool.fetchrow(
                "SELECT uploader_id FROM video_uploads "
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
            "UPDATE video_uploads SET status = 'denied', denied_by = $2, "
            "denial_reason = $3, reviewed_at = now() "
            "WHERE id = $1 AND status = 'pending' "
            "RETURNING uploader_id, review_message_id",
            upload_id,
            interaction.user.id,
            reason[:1_000],
        )
        if row is None:
            return False
        uploader_id = int(row["uploader_id"])
        if block:
            await self.bot.pool.execute(
                "INSERT INTO video_upload_blocks (user_id, blocked_by) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET blocked_by = EXCLUDED.blocked_by, "
                "blocked_at = now()",
                uploader_id,
                interaction.user.id,
            )
            message = (
                "Your video submission was denied and you have been blocked from "
                "submitting videos to Fishie."
            )
        else:
            message = f"Your video submission was denied.\nReason: {_safe_text(reason)}"
        await self._dm_uploader(uploader_id, message)
        return True

    async def _video_alias_row(
        self, user_id: int | None, identifier: str
    ) -> Any | None:
        if user_id is None:
            return None
        return await self.bot.pool.fetchrow(
            "SELECT v.id, v.library_id, v.source_url, v.filename, v.review_message_id "
            "FROM video_aliases a "
            "JOIN video_uploads v ON v.id = a.video_id "
            "WHERE a.user_id = $1 AND lower(btrim(a.alias)) = lower(btrim($2)) "
            "AND v.status = 'approved' AND v.library_id IS NOT NULL "
            "AND lower(v.filename) NOT LIKE '%.gif'",
            user_id,
            identifier,
        )

    @staticmethod
    def _video_pool_filter(user_id: int | None) -> tuple[str, tuple[int, ...]]:
        """Return requester-scoped exclusions for random library selection."""

        if user_id is None:
            return "", ()
        return (
            " AND NOT EXISTS ("
            "SELECT 1 FROM video_library_blocks b "
            "WHERE b.user_id = $1 AND b.blocked_uploader_id = v.uploader_id)"
            " AND NOT EXISTS ("
            "SELECT 1 FROM video_library_hides h "
            "WHERE h.user_id = $1 AND h.video_id = v.id)",
            (user_id,),
        )

    async def _send_video(self, ctx: Context, identifier: str | None = None) -> None:
        requester_id = getattr(getattr(ctx, "author", None), "id", None)
        identifier = identifier.strip() if identifier is not None else None
        explicit_identifier = identifier is not None
        video_id: int | None = None
        row = (
            await self._video_alias_row(requester_id, identifier)
            if identifier
            else None
        )
        if row is None and identifier is not None:
            try:
                video_id = int(identifier)
            except ValueError as error:
                raise commands.BadArgument(
                    "That video ID or personal video alias was not found."
                ) from error
            if video_id < 1:
                raise commands.BadArgument("The video ID must be positive.")

        if row is None and video_id is None:
            pool_filter, filter_args = self._video_pool_filter(requester_id)
            if requester_id is None:
                count_query = (
                    "SELECT COUNT(*) FROM video_uploads v "
                    "WHERE v.status = 'approved' AND v.library_id IS NOT NULL "
                    "AND lower(v.filename) NOT LIKE '%.gif'"
                )
            else:
                count_query = (
                    "SELECT COUNT(*) FROM video_uploads v "
                    "WHERE v.status = 'approved' AND v.library_id IS NOT NULL "
                    "AND lower(v.filename) NOT LIKE '%.gif'" + pool_filter
                )
            approved_count = int(
                await self.bot.pool.fetchval(count_query, *filter_args) or 0
            )
            if approved_count:
                offset = secrets.randbelow(approved_count)
                if requester_id is None:
                    row = await self.bot.pool.fetchrow(
                        "SELECT v.id, v.library_id, v.source_url, v.filename, v.review_message_id "
                        "FROM video_uploads v WHERE v.status = 'approved' "
                        "AND v.library_id IS NOT NULL "
                        "AND lower(v.filename) NOT LIKE '%.gif' "
                        "ORDER BY v.library_id OFFSET $1 LIMIT 1",
                        offset,
                    )
                else:
                    row = await self.bot.pool.fetchrow(
                        "SELECT v.id, v.library_id, v.source_url, v.filename, v.review_message_id "
                        "FROM video_uploads v WHERE v.status = 'approved' "
                        "AND v.library_id IS NOT NULL "
                        "AND lower(v.filename) NOT LIKE '%.gif'"
                        + pool_filter
                        + " ORDER BY v.library_id OFFSET $2 LIMIT 1",
                        requester_id,
                        offset,
                    )
            if row is None and approved_count:
                # A concurrent deletion can make the randomly selected offset
                # disappear between the count and lookup. Retry with the first
                # remaining row rather than reporting an empty library.
                if requester_id is None:
                    row = await self.bot.pool.fetchrow(
                        "SELECT v.id, v.library_id, v.source_url, v.filename, v.review_message_id "
                        "FROM video_uploads v WHERE v.status = 'approved' "
                        "AND v.library_id IS NOT NULL "
                        "AND lower(v.filename) NOT LIKE '%.gif' "
                        "ORDER BY v.library_id LIMIT 1"
                    )
                else:
                    row = await self.bot.pool.fetchrow(
                        "SELECT v.id, v.library_id, v.source_url, v.filename, v.review_message_id "
                        "FROM video_uploads v WHERE v.status = 'approved' "
                        "AND v.library_id IS NOT NULL "
                        "AND lower(v.filename) NOT LIKE '%.gif'"
                        + pool_filter
                        + " ORDER BY v.library_id LIMIT 1",
                        requester_id,
                    )
        elif row is None:
            row = await self.bot.pool.fetchrow(
                "SELECT id, library_id, source_url, filename, review_message_id "
                "FROM video_uploads "
                "WHERE status = 'approved' AND library_id = $1 "
                "AND lower(filename) NOT LIKE '%.gif'",
                video_id,
            )
        if row is None:
            await ctx.send(
                (
                    "That video does not exist or has not been approved."
                    if explicit_identifier
                    else (
                        "There are no approved videos available in your pool."
                        if requester_id is not None
                        else "There are no approved videos yet."
                    )
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        source_url = await self._current_video_url(
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
                "That video has no usable Discord attachment yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Fishie video"
                    if explicit_identifier
                    else "## Random Fishie video"
                ),
                discord.ui.Separator(),
                discord.ui.MediaGallery(discord.MediaGalleryItem(source_url)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "Upload your own video with `fish video upload` · "
                    f"Library ID `{int(row.get('library_id') or row['id'])}`"
                ),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @cast(Any, commands.hybrid_group)(
        name="video",
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        identifier="An approved library ID or one of your personal video aliases."
    )
    async def video(self, ctx: Context, *, identifier: str | None = None) -> None:
        """Show a random approved Fishie video, library ID, or personal alias."""
        async with ctx.typing():
            await self._send_video(ctx, identifier)

    @video.command(name="random")
    @app_commands.describe(
        identifier="An optional approved video library ID or personal alias."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_random(self, ctx: Context, identifier: str | None = None) -> None:
        """Show a random approved Fishie video or a library ID."""
        async with ctx.typing():
            await self._send_video(ctx, identifier)

    async def _send_video_uploads(
        self, ctx: Context, target: str | None = None
    ) -> None:
        filters = await resolve_library_filters(ctx, target)
        condition = video_media_condition(filters.media, alias="v")
        query = (
            "SELECT v.id, v.library_id, v.source_url, v.filename, "
            "v.review_message_id, v.uploader_id "
            "FROM video_uploads AS v WHERE v.status = 'approved' "
            "AND v.library_id IS NOT NULL"
            f"{condition}"
        )
        if filters.all_users:
            query += " ORDER BY v.library_id"
            rows = await self.bot.pool.fetch(query)
            title = "All Fishie video uploads"
        else:
            query += " AND v.uploader_id = $1 ORDER BY v.library_id"
            rows = await self.bot.pool.fetch(query, filters.user_id)
            title = (
                "Your Fishie video uploads"
                if filters.user_id == ctx.author.id
                else "Fishie video uploads"
            )
        source = VideoUploadsPageSource(
            self,
            ctx,
            [dict(row) for row in rows],
            title=title,
        )
        pager = LayoutPager(
            source,
            ctx=ctx,
            accent_color=self.bot.embedcolor,
            timeout=600,
        )
        await pager.start()

    async def _send_video_stats(self, ctx: Context) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT uploader_id, COUNT(*) AS total FROM video_uploads "
            "WHERE status = 'approved' AND library_id IS NOT NULL "
            "AND lower(filename) NOT LIKE '%.gif' "
            "GROUP BY uploader_id "
            "ORDER BY total DESC, uploader_id ASC LIMIT 10"
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
                f"**{_safe_text(username, limit=100)}** (`{user_id}`) · "
                f"{int(row['total']):,} video(s)"
            )
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Fishie video upload stats"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "\n".join(lines) if lines else "No approved video uploads yet."
                ),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @video.command(name="alias")
    @app_commands.describe(
        identifier="The approved video library ID.",
        alias="Your personal name for the video.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_alias(self, ctx: Context, identifier: int, *, alias: str) -> None:
        """Give an approved video a personal name for random library access."""

        alias_text = " ".join(alias.split()).strip()
        if not alias_text or len(alias_text) > 100:
            raise commands.BadArgument("Your video alias must be 1 to 100 characters.")
        row = await self.bot.pool.fetchrow(
            "SELECT id, library_id FROM video_uploads "
            "WHERE library_id = $1 AND status = 'approved' "
            "AND lower(filename) NOT LIKE '%.gif'",
            identifier,
        )
        if row is None:
            await ctx.send(
                "That video does not exist or has not been approved.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        conflict = await self.bot.pool.fetchrow(
            "SELECT video_id FROM video_aliases "
            "WHERE user_id = $1 AND lower(btrim(alias)) = lower(btrim($2))",
            ctx.author.id,
            alias_text,
        )
        if conflict is not None and int(conflict["video_id"]) != int(row["id"]):
            await ctx.send(
                "You already use that alias for another video.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO video_aliases (video_id, user_id, alias) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, video_id) DO UPDATE SET alias = EXCLUDED.alias, "
            "created_at = now()",
            int(row["id"]),
            ctx.author.id,
            alias_text,
        )
        await ctx.send(
            f"Video `{identifier}` is now available as `{_safe_text(alias_text)}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @video.command(name="block")
    @app_commands.describe(
        user="Uploader whose videos should be hidden from your random pool."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_block(self, ctx: Context, user: discord.User) -> None:
        """Hide another uploader's videos from your random library results."""

        if user.id == ctx.author.id:
            await ctx.send(
                "You cannot block your own videos.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO video_library_blocks (user_id, blocked_uploader_id) "
            "VALUES ($1, $2) ON CONFLICT DO NOTHING",
            ctx.author.id,
            user.id,
        )
        await ctx.send(
            f"Videos uploaded by `{_safe_text(user.name)}` are now hidden from your random pool.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @video.command(name="unblock")
    @app_commands.describe(
        user="Uploader whose videos should be shown in your random pool again."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_unblock(self, ctx: Context, user: discord.User) -> None:
        """Show an uploader's videos in your random library results again."""

        result = await self.bot.pool.execute(
            "DELETE FROM video_library_blocks WHERE user_id = $1 "
            "AND blocked_uploader_id = $2",
            ctx.author.id,
            user.id,
        )
        await ctx.send(
            (
                f"Videos uploaded by `{_safe_text(user.name)}` are no longer hidden."
                if result.endswith(" 1")
                else "That uploader was not blocked in your video pool."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @video.command(name="hide")
    @app_commands.describe(
        identifier="Approved video library ID to hide from your random pool."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_hide(self, ctx: Context, identifier: int) -> None:
        """Hide one approved video from your random library results."""

        row = await self.bot.pool.fetchrow(
            "SELECT id FROM video_uploads "
            "WHERE library_id = $1 AND status = 'approved'",
            identifier,
        )
        if row is None:
            await ctx.send(
                "That video does not exist or has not been approved.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO video_library_hides (user_id, video_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            ctx.author.id,
            int(row["id"]),
        )
        await ctx.send(
            f"Video `{identifier}` is now hidden from your random pool.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @video.command(name="unhide")
    @app_commands.describe(
        identifier="Approved video library ID to show in your random pool again."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_unhide(self, ctx: Context, identifier: int) -> None:
        """Show a hidden video in your random library results again."""

        result = await self.bot.pool.execute(
            "DELETE FROM video_library_hides AS hidden USING video_uploads AS upload "
            "WHERE hidden.user_id = $1 AND hidden.video_id = upload.id "
            "AND upload.library_id = $2",
            ctx.author.id,
            identifier,
        )
        await ctx.send(
            (
                f"Video `{identifier}` is no longer hidden."
                if result.endswith(" 1")
                else "That video was not hidden in your pool."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @video.command(name="stats")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_stats(self, ctx: Context) -> None:
        """Show the users with the most approved Fishie video uploads."""

        async with ctx.typing():
            await self._send_video_stats(ctx)

    @video.command(name="uploads")
    @app_commands.describe(
        target="A media filter, Discord user, `all`, or any combination."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_uploads(self, ctx: Context, *, target: str | None = None) -> None:
        """Browse approved uploads for a user or the full video library."""

        async with ctx.typing():
            await self._send_video_uploads(ctx, target)

    @cast(Any, commands.command)(name="videos")
    async def videos(self, ctx: Context, *, target: str | None = None) -> None:
        """Browse approved video uploads with optional media and user filters."""

        async with ctx.typing():
            await self._send_video_uploads(ctx, target)

    @video.command(name="upload")
    @app_commands.describe(
        video="A public video file or supported download-page URL to submit for review.",
        attachment="A video attachment to submit for review.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_upload(
        self,
        ctx: Context,
        video: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> None:
        """Submit a video URL, attachment, or replied-to video for review."""
        lock = manual_upload_lock(ctx)
        if lock.locked():
            await ctx.send(
                "Another upload is already being processed for you. Please wait for it to finish.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with lock:
            async with ctx.typing():
                await self._video_upload_impl(ctx, video, attachment)

    async def _video_upload_impl(
        self,
        ctx: Context,
        video: str | None = None,
        attachment: discord.Attachment | None = None,
        *,
        _allow_message_attachments: bool = True,
        _enforce_rate_limit: bool = True,
    ) -> None:
        """Submit a video URL or attachment to the Fishie review channel."""
        blocked = await self.bot.pool.fetchval(
            "SELECT 1 FROM video_upload_blocks WHERE user_id = $1", ctx.author.id
        )
        if blocked:
            await ctx.send(
                "You are blocked from submitting videos to Fishie.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        video_url = video.strip() if video else None
        video_url = normalize_download_url(video_url) if video_url else None

        # A text command can carry several attachments even though an app
        # command can only expose one attachment option.  Process every
        # attachment as its own review submission, while de-duplicating the
        # explicit option when Discord also exposes it on ``ctx.message``.
        message_attachments: tuple[discord.Attachment, ...] = ()
        if _allow_message_attachments:
            message = getattr(ctx, "message", None)
            message_attachments = tuple(
                candidate
                for candidate in getattr(message, "attachments", ())
                if isinstance(candidate, discord.Attachment)
            )
        attachment_candidates: list[discord.Attachment] = []
        seen_attachment_ids: set[int] = set()
        for candidate in ((attachment,) if attachment is not None else ()) + (
            message_attachments
        ):
            candidate_id = int(getattr(candidate, "id", 0) or 0)
            if candidate_id and candidate_id in seen_attachment_ids:
                continue
            if candidate_id:
                seen_attachment_ids.add(candidate_id)
            attachment_candidates.append(candidate)

        if video_url and attachment_candidates:
            await ctx.send(
                "Provide either a direct video URL or an attachment, not both.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if video_url is None and len(attachment_candidates) > 1:
            if _enforce_rate_limit:
                retry_after = consume_manual_uploads(ctx, len(attachment_candidates))
                if retry_after:
                    await ctx.send(
                        f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
            for candidate in attachment_candidates:
                await self._video_upload_impl(
                    ctx,
                    attachment=candidate,
                    _allow_message_attachments=False,
                    _enforce_rate_limit=False,
                )
            return
        if video_url is None and attachment_candidates:
            attachment = attachment_candidates[0]
        if video_url is None and attachment is None:
            # Only inspect an explicitly replied-to message.  Unlike the
            # download and media-effect commands, video upload must not scan
            # recent channel history for an unrelated source.
            replied = await self._reply_message(ctx)
            if replied is not None and self._is_library_response(replied):
                await ctx.send(
                    "no",
                    delete_after=3,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            reply_attachments, reply_url = await self._reply_video_sources(ctx, replied)
            if len(reply_attachments) > 1:
                if _enforce_rate_limit:
                    retry_after = consume_manual_uploads(ctx, len(reply_attachments))
                    if retry_after:
                        await ctx.send(
                            f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                for candidate in reply_attachments:
                    await self._video_upload_impl(
                        ctx,
                        attachment=candidate,
                        _allow_message_attachments=False,
                        _enforce_rate_limit=False,
                    )
                return
            if reply_attachments:
                attachment = reply_attachments[0]
            elif reply_url is not None:
                video_url = reply_url
        if video_url is None and attachment is None:
            await ctx.send(
                "Provide a direct video URL, reply to a video message, or attach a video file when using this command.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if _enforce_rate_limit:
            retry_after = consume_manual_uploads(ctx)
            if retry_after:
                await ctx.send(
                    f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        if attachment is not None and not self._is_video(attachment):
            await ctx.send(
                "That attachment is not a supported video file.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if attachment is not None and attachment.size > VIDEO_MAX_BYTES:
            await ctx.send(
                "Videos must be 500 MB or smaller.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        target = await self._video_review_channel()
        if target is None:
            await ctx.send(
                "The Fishie video review channel is unavailable right now.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        review_limit = int(getattr(target.guild, "filesize_limit", 0) or 0)
        temporary_path: Path | None = None
        resized_path: Path | None = None
        upload_path: Path | None = None
        if video_url is not None:
            try:
                (
                    temporary_path,
                    filename,
                    video_size,
                    source_url,
                ) = await self._download_video_source(ctx, video_url)
            except commands.BadArgument as error:
                await ctx.send(
                    str(error), allowed_mentions=discord.AllowedMentions.none()
                )
                return
            filename = _random_video_filename(filename)
        else:
            assert attachment is not None
            filename = _random_video_filename(attachment.filename)
            video_size = attachment.size
            source_url = attachment.url
        upload_path = temporary_path

        if await self._approved_source_exists(source_url):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            await ctx.send(
                "That video is already in the Fishie video library.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if review_limit and video_size > review_limit:
            if video_size < VIDEO_RESIZE_THRESHOLD:
                if upload_path is None:
                    assert attachment is not None
                    suffix = Path(attachment.filename).suffix or ".upload"
                    with tempfile.NamedTemporaryFile(
                        prefix="fishie-video-attachment-",
                        suffix=suffix,
                        delete=False,
                    ) as handle:
                        temporary_path = Path(handle.name)
                    try:
                        await attachment.save(temporary_path)
                        video_size = temporary_path.stat().st_size
                        upload_path = temporary_path
                    except (discord.HTTPException, OSError):
                        temporary_path.unlink(missing_ok=True)
                        temporary_path = None

                if upload_path is not None:
                    is_gif = Path(filename).suffix.casefold() == ".gif"
                    for scale_percent in (90, 80, 70, 60, 50):
                        candidate = await self._resize_video_to_scale(
                            upload_path,
                            gif=is_gif,
                            scale_percent=scale_percent,
                        )
                        if candidate is None:
                            continue
                        try:
                            candidate_size = candidate.stat().st_size
                        except OSError:
                            candidate.unlink(missing_ok=True)
                            continue
                        if candidate_size <= review_limit:
                            resized_path = candidate
                            upload_path = candidate
                            video_size = candidate_size
                            filename = _random_video_filename(
                                "resized.gif" if is_gif else "resized.mp4"
                            )
                            break
                        candidate.unlink(missing_ok=True)

            if upload_path is None or video_size > review_limit:
                limit_mb = review_limit / (1024 * 1024)
                if resized_path is not None:
                    resized_path.unlink(missing_ok=True)
                    resized_path = None
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                    temporary_path = None
                await ctx.send(
                    f"That video is too large ({limit_mb:g} MB upload limit).",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        source_guild = ctx.guild
        source_text = (
            f"{_safe_text(source_guild.name)} (`{source_guild.id}`)"
            if source_guild is not None
            else "Direct message"
        )
        approved, denied = await self._video_counts(ctx.author.id)
        review_file: discord.File | None = None
        try:
            row = await self.bot.pool.fetchrow(
                "INSERT INTO video_uploads "
                "(source_url, filename, uploader_id, source_guild_id, source_channel_id) "
                "VALUES (NULL, $1, $2, $3, $4) RETURNING id",
                filename,
                ctx.author.id,
                source_guild.id if source_guild is not None else None,
                getattr(ctx.channel, "id", None),
            )
            if row is None:
                raise RuntimeError("Could not create the pending video submission.")
            upload_id = int(row["id"])
            protected = await self._uploader_has_protected_role(ctx.author.id)
            review_view = VideoReviewView(
                self,
                upload_id,
                block_disabled=protected is not False,
            )
            embed = discord.Embed(
                title="Video submission",
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
            else:
                assert attachment is not None
                review_file = await attachment.to_file(use_cached=False, spoiler=False)
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
                    "Video review message %s returned without attachment metadata; "
                    "deferring attachment lookup until approval",
                    review_message.id,
                )
            await self.bot.pool.execute(
                "UPDATE video_uploads SET review_message_id = $2 WHERE id = $1",
                upload_id,
                review_message.id,
            )
        except Exception:
            if "upload_id" in locals():
                await self.bot.pool.execute(
                    "DELETE FROM video_uploads WHERE id = $1", upload_id
                )
            raise
        finally:
            if review_file is not None:
                review_file.close()
            if resized_path is not None:
                resized_path.unlink(missing_ok=True)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        await ctx.send(
            "Your video was sent for review. I will notify you by DM when it is approved or denied.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @video.command(name="delete")
    @app_commands.describe(
        identifier="Your approved library ID or `all`; the bot owner can clear the full library."
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_delete(self, ctx: Context, *, identifier: str) -> None:
        """Delete an approved video you uploaded or clear your own uploads."""
        is_owner = self._is_bot_owner(ctx.author.id)
        if identifier.casefold() == "all":
            query = (
                "SELECT id, review_message_id FROM video_uploads "
                "WHERE status = 'approved' AND library_id IS NOT NULL"
            )
            query_args: tuple[object, ...] = ()
            if not is_owner:
                query += " AND uploader_id = $1"
                query_args = (ctx.author.id,)
            rows = await self.bot.pool.fetch(
                query + " ORDER BY library_id", *query_args
            )
            if not rows:
                await ctx.send(
                    (
                        "You do not have any approved videos to delete."
                        if not is_owner
                        else "There are no approved videos to delete."
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            confirmation = await ctx.prompt(
                f"This will permanently delete {len(rows):,} approved video(s). Continue?",
                confirm_label="Delete",
                cancel_label="Cancel",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if confirmation is None:
                await ctx.send(
                    "Video deletion cancelled.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            removable_ids: list[int] = []
            failed = 0
            for row in rows:
                removed = await self._delete_review_message(row["review_message_id"])
                if removed:
                    removable_ids.append(int(row["id"]))
                else:
                    failed += 1
            if removable_ids:
                await self.bot.pool.execute(
                    "INSERT INTO video_library_deleted_ids (library_id) "
                    "SELECT library_id FROM video_uploads "
                    "WHERE id = ANY($1::BIGINT[]) AND status = 'approved' "
                    "AND library_id IS NOT NULL ON CONFLICT DO NOTHING",
                    removable_ids,
                )
                delete_query = (
                    "DELETE FROM video_uploads WHERE id = ANY($1::BIGINT[]) "
                    "AND status = 'approved'"
                )
                delete_args: tuple[object, ...] = (removable_ids,)
                if not is_owner:
                    delete_query += " AND uploader_id = $2"
                    delete_args += (ctx.author.id,)
                await self.bot.pool.execute(delete_query, *delete_args)
            response = (
                f"Deleted {len(removable_ids):,} approved video(s)."
                if is_owner
                else f"Deleted {len(removable_ids):,} of your approved video(s)."
            )
            if failed:
                response += (
                    f" Kept {failed:,} video(s) because their review messages "
                    "could not be deleted."
                )
            await ctx.send(
                response,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            upload_id = int(identifier)
        except ValueError as error:
            alias_row = await self._video_alias_row(ctx.author.id, identifier)
            if alias_row is None:
                raise commands.BadArgument(
                    "Provide a library ID, personal alias, or `all`."
                ) from error
            upload_id = int(alias_row["id"])
            library_id = int(alias_row["library_id"])
            alias_label = _safe_text(identifier.strip(), limit=100)
            confirmation = await ctx.prompt(
                f"You are removing the alias `{alias_label}` for library video "
                f"`{library_id}`. This only removes the alias. If you own the "
                "video and want to delete the upload, run "
                f"`fish video delete {library_id}` instead. Continue?",
                confirm_label="Delete alias",
                cancel_label="Cancel",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if confirmation is None:
                await ctx.send(
                    "Alias deletion cancelled.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            result = await self.bot.pool.execute(
                "DELETE FROM video_aliases WHERE user_id = $1 AND video_id = $2",
                ctx.author.id,
                upload_id,
            )
            await ctx.send(
                (
                    f"Removed alias `{alias_label}` for library video `{library_id}`. "
                    "The uploaded video was not deleted."
                    if result.endswith(" 1")
                    else "That video alias no longer exists."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        row = await self.bot.pool.fetchrow(
            "SELECT id, library_id, uploader_id, review_message_id "
            "FROM video_uploads WHERE library_id = $1 AND status = 'approved'",
            upload_id,
        )
        if row is None:
            await ctx.send(
                "No approved video had that ID.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not is_owner and int(row["uploader_id"]) != ctx.author.id:
            await ctx.send(
                "You can only delete videos that you uploaded.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        confirmation = await ctx.prompt(
            f"Permanently delete approved video `{int(row['library_id'])}`?",
            confirm_label="Delete",
            cancel_label="Cancel",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if confirmation is None:
            await ctx.send(
                "Video deletion cancelled.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._delete_review_message(row["review_message_id"]):
            await ctx.send(
                "That video's review message could not be deleted, so the library "
                "entry was kept. Try again later.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.bot.pool.execute(
            "INSERT INTO video_library_deleted_ids (library_id) VALUES ($1) "
            "ON CONFLICT DO NOTHING",
            int(row["library_id"]),
        )
        delete_query = "DELETE FROM video_uploads WHERE id = $1 AND status = 'approved'"
        delete_args: tuple[object, ...] = (upload_id,)
        if not is_owner:
            delete_query += " AND uploader_id = $2"
            delete_args += (ctx.author.id,)
        result = await self.bot.pool.execute(delete_query, *delete_args)
        await ctx.send(
            (
                "Deleted that approved video."
                if result.endswith(" 1")
                else "No approved video had that ID."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
