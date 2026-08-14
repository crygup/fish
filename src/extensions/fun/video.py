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

from utils.network import (
    REDIRECT_STATUSES,
    validate_connected_peer,
    validate_public_url,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


# These are deliberately kept as constants until the review channel is moved
# into the operator configuration.  The source guild is not a permission
# boundary.  Users may submit from a DM or another guild, but reviews always
# go to this private moderation channel.
VIDEO_REVIEW_GUILD_ID = 939497177821110272
VIDEO_REVIEW_CHANNEL_ID = 1536730398292181042
VIDEO_MAX_BYTES = 500 * 1024 * 1024
VIDEO_RESIZE_THRESHOLD = 15 * 1024 * 1024
VIDEO_EXTENSIONS = {".avi", ".gif", ".m4v", ".mov", ".mp4", ".mkv", ".webm"}
VIDEO_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=300,
    connect=15,
    sock_read=60,
)
VIDEO_RESIZE_TIMEOUT = 60


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
        await interaction.response.defer(ephemeral=True)
        reason = str(self.reason.value).strip() or "No reason was provided."
        result = await self.view_ref.cog._deny_video(
            interaction,
            self.view_ref.upload_id,
            reason,
            block=False,
        )
        if result:
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

    def __init__(self, cog: "VideoCommands", upload_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.upload_id = upload_id
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

    async def approve(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        approved = await self.cog._approve_video(interaction, self.upload_id)
        self.disable_all()
        if approved:
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
        await interaction.response.send_modal(VideoDenyModal(self))

    async def block(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        blocked = await self.cog._deny_video(
            interaction,
            self.upload_id,
            "The uploader has been blocked from submitting videos.",
            block=True,
        )
        self.disable_all()
        if not blocked:
            await interaction.followup.send(
                "That submission has already been reviewed.", ephemeral=True
            )
        self.stop()


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
            "SELECT id, review_message_id FROM video_uploads "
            "WHERE status = 'pending' AND review_message_id IS NOT NULL"
        )
        for row in rows:
            try:
                upload_id = int(row["id"])
                view = VideoReviewView(self, upload_id)
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
        return bool(member.guild_permissions.manage_guild)

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
        return content_type.startswith("video/") or suffix in VIDEO_EXTENSIONS

    @staticmethod
    def _is_video_response(content_type: str, filename: str) -> bool:
        """Accept video responses and common CDN responses for video files."""

        normalized = content_type.casefold().split(";", 1)[0].strip()
        suffix = Path(filename).suffix.casefold()
        if normalized.startswith("video/"):
            return True
        # GIFs are image media but are valid entries in the video library.
        if suffix == ".gif" and normalized == "image/gif":
            return True
        # Some attachment/CDN endpoints use a generic content type.
        return suffix in VIDEO_EXTENSIONS and normalized in {
            "",
            "application/octet-stream",
            "binary/octet-stream",
        }

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

        current = url.strip().strip("<>")
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
                    with temporary.open("wb") as output:
                        async for chunk in response.content.iter_chunked(64 * 1024):
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
                f"scale=trunc(iw*{scale:g}/2)*2:"
                f"trunc(ih*{scale:g}/2)*2:flags=lanczos"
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
            return source_url
        channel = await self._video_review_channel()
        if channel is None:
            return source_url
        try:
            message = await channel.fetch_message(review_message_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            self.bot.logger.warning(
                "Could not refresh attachment URL for approved video %s",
                upload_id,
                exc_info=True,
            )
            return source_url
        if not message.attachments:
            self.bot.logger.warning(
                "Approved video %s review message %s has no attachment",
                upload_id,
                review_message_id,
            )
            return source_url

        current_url = message.attachments[0].url
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
    ) -> bool:
        row = await self.bot.pool.fetchrow(
            "UPDATE video_uploads SET status = 'approved', approved_by = $2, "
            "reviewed_at = now() WHERE id = $1 AND status = 'pending' "
            "RETURNING uploader_id, source_url",
            upload_id,
            interaction.user.id,
        )
        if row is not None:
            await self._dm_approved_video(
                int(row["uploader_id"]),
                upload_id,
                str(row["source_url"]),
            )
        return row is not None

    async def _deny_video(
        self,
        interaction: discord.Interaction,
        upload_id: int,
        reason: str,
        *,
        block: bool,
    ) -> bool:
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
        await self._delete_review_message(row["review_message_id"])
        return True

    async def _send_video(self, ctx: Context, identifier: str | None = None) -> None:
        video_id: int | None = None
        if identifier is not None:
            try:
                video_id = int(identifier)
            except ValueError as error:
                raise commands.BadArgument("The video ID must be a number.") from error
            if video_id < 1:
                raise commands.BadArgument("The video ID must be positive.")

        if video_id is None:
            approved_count = int(
                await self.bot.pool.fetchval(
                    "SELECT COUNT(*) FROM video_uploads WHERE status = 'approved'"
                )
                or 0
            )
            row = (
                await self.bot.pool.fetchrow(
                    "SELECT id, source_url, filename, review_message_id "
                    "FROM video_uploads WHERE status = 'approved' ORDER BY id "
                    "OFFSET $1 LIMIT 1",
                    secrets.randbelow(approved_count),
                )
                if approved_count
                else None
            )
            if row is None and approved_count:
                # A concurrent deletion can make the randomly selected offset
                # disappear between the count and lookup. Retry with the first
                # remaining row rather than reporting an empty library.
                row = await self.bot.pool.fetchrow(
                    "SELECT id, source_url, filename, review_message_id "
                    "FROM video_uploads WHERE status = 'approved' "
                    "ORDER BY id LIMIT 1"
                )
        else:
            row = await self.bot.pool.fetchrow(
                "SELECT id, source_url, filename, review_message_id "
                "FROM video_uploads "
                "WHERE status = 'approved' AND id = $1",
                video_id,
            )
        if row is None:
            await ctx.send(
                (
                    "That video does not exist or has not been approved."
                    if video_id is not None
                    else "There are no approved videos yet."
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
        view = discord.ui.LayoutView(timeout=300)
        view.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "## Fishie video"
                    if video_id is not None
                    else "## Random Fishie video"
                ),
                discord.ui.Separator(),
                discord.ui.MediaGallery(discord.MediaGalleryItem(source_url)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "Upload your own video with `fish video upload` · "
                    f"Library ID `{int(row['id'])}`"
                ),
                accent_color=self.bot.embedcolor,
            )
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @cast(Any, commands.hybrid_group)(
        name="video",
        aliases=("videos",),
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video(self, ctx: Context, identifier: str | None = None) -> None:
        """Show a random approved Fishie video or a library ID."""
        async with ctx.typing():
            await self._send_video(ctx, identifier)

    @video.command(name="random")
    @app_commands.describe(identifier="An optional approved video library ID.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_random(self, ctx: Context, identifier: str | None = None) -> None:
        """Show a random approved Fishie video or a library ID."""
        async with ctx.typing():
            await self._send_video(ctx, identifier)

    @video.command(name="upload")
    @app_commands.describe(
        video="A direct public video URL to submit for review.",
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
        """Submit a video URL or attachment for review."""
        async with ctx.typing():
            await self._video_upload_impl(ctx, video, attachment)

    async def _video_upload_impl(
        self,
        ctx: Context,
        video: str | None = None,
        attachment: discord.Attachment | None = None,
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
        video_url = video_url or None
        if video_url and attachment is not None:
            await ctx.send(
                "Provide either a direct video URL or an attachment, not both.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if video_url is None:
            message = getattr(ctx, "message", None)
            attachments = getattr(message, "attachments", ())
            attachment = next(iter(attachments), None)
        if video_url is None and attachment is None:
            await ctx.send(
                "Provide a direct video URL or attach a video file when using this command.",
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
                temporary_path, filename, video_size, source_url = (
                    await self._download_video_url(video_url)
                )
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
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                source_url,
                filename,
                ctx.author.id,
                source_guild.id if source_guild is not None else None,
                getattr(ctx.channel, "id", None),
            )
            if row is None:
                raise RuntimeError("Could not create the pending video submission.")
            upload_id = int(row["id"])
            review_view = VideoReviewView(self, upload_id)
            embed = discord.Embed(
                title="Video submission",
                description=(
                    f"**Uploader:** {_safe_text(ctx.author.name)} (`{ctx.author.id}`)\n"
                    f"**Source guild:** {source_text}\n"
                    f"**Approved:** {approved:,} · **Denied:** {denied:,}\n"
                    f"**Library ID:** `{upload_id}`"
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
            source_url = (
                review_message.attachments[0].url
                if review_message.attachments
                else source_url
            )
            await self.bot.pool.execute(
                "UPDATE video_uploads SET source_url = $2, review_message_id = $3 "
                "WHERE id = $1",
                upload_id,
                source_url,
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
    async def video_delete(self, ctx: Context, identifier: str) -> None:
        """Delete an approved video you uploaded or clear your own uploads."""
        is_owner = self._is_bot_owner(ctx.author.id)
        if identifier.casefold() == "all":
            query = (
                "SELECT id, review_message_id FROM video_uploads "
                "WHERE status = 'approved'"
            )
            query_args: tuple[object, ...] = ()
            if not is_owner:
                query += " AND uploader_id = $1"
                query_args = (ctx.author.id,)
            rows = await self.bot.pool.fetch(query + " ORDER BY id", *query_args)
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
            raise commands.BadArgument("Provide a library ID or `all`.") from error
        row = await self.bot.pool.fetchrow(
            "SELECT id, uploader_id, review_message_id FROM video_uploads "
            "WHERE id = $1 AND status = 'approved'",
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
            f"Permanently delete approved video `{upload_id}`?",
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

    @video.command(name="unlock")
    @commands.is_owner()
    @app_commands.describe(user="The user allowed to submit videos again.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def video_unlock(self, ctx: Context, user: discord.User) -> None:
        """Allow a blocked user to submit videos again."""
        result = await self.bot.pool.execute(
            "DELETE FROM video_upload_blocks WHERE user_id = $1", user.id
        )
        await ctx.send(
            (
                "The user can submit videos again."
                if result.endswith(" 1")
                else "That user was not blocked."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
