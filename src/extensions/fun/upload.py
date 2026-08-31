"""Unified text-only media uploads for the Fishie review libraries."""

from __future__ import annotations

import asyncio
import mimetypes
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlsplit

import discord
from discord.ext import commands

from utils.converters import MediaConverter
from utils.downloads import (
    Downloader,
    is_downloadable_media_page,
    normalize_download_url,
)
from utils.errors import DownloadError

from .post import POST_EXTENSIONS, PostCommands
from .upload_limits import consume_manual_uploads, manual_upload_lock
from .video import VIDEO_EXTENSIONS, VideoCommands

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")
_VIDEO_SIGNATURES = (b"\x1a\x45\xdf\xa3",)
_MAX_PROBE_BYTES = 500 * 1024 * 1024


class LocalMediaAttachment:
    """Attachment-shaped wrapper for media downloaded while classifying a URL."""

    def __init__(
        self,
        path: Path,
        *,
        filename: str,
        source_url: str,
        content_type: str,
        size: int,
    ) -> None:
        self.path = path
        self.filename = filename
        self.url = source_url
        self.content_type = content_type
        self.size = size

    async def save(
        self,
        destination: str | Path,
        *,
        seek_begin: bool = True,
        use_cached: bool = False,
    ) -> None:
        """Match the subset of ``discord.Attachment.save`` used by uploads."""

        del seek_begin, use_cached
        destination_path = Path(destination)
        await asyncio.to_thread(shutil.copyfile, self.path, destination_path)

    async def to_file(
        self,
        *,
        use_cached: bool = False,
        spoiler: bool = False,
    ) -> discord.File:
        del use_cached
        return discord.File(self.path, filename=self.filename, spoiler=spoiler)


class UploadCommands:
    """Expose one text command that routes media to the right review library."""

    bot: Fishie

    @staticmethod
    def _kind_from_filename(filename: str) -> str | None:
        suffix = Path(unquote(filename)).suffix.casefold()
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in POST_EXTENSIONS:
            return "post"
        return None

    @classmethod
    def _kind_from_bytes(cls, data: bytes, filename: str) -> str | None:
        if data.startswith(_GIF_SIGNATURES):
            return "post"
        if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            return "post"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "post"
        # AVIF is an ISO-BMFF image and therefore also contains an ``ftyp``
        # box. Check its brand before the generic video-container branch so
        # direct AVIF URLs are routed to the post upload path.
        if (
            len(data) >= 12
            and data[4:8] == b"ftyp"
            and data[8:12]
            in {
                b"avif",
                b"avis",
            }
        ):
            return "post"
        if (
            data.startswith(_VIDEO_SIGNATURES)
            or data[4:8] == b"ftyp"
            or (data.startswith(b"RIFF") and data[8:12] in {b"AVI ", b"WAVE"})
        ):
            return "video"
        return cls._kind_from_filename(filename)

    @classmethod
    def _kind_from_url(cls, url: str) -> str | None:
        kind = cls._kind_from_filename(urlsplit(url).path)
        if kind is not None:
            return kind
        hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
        if hostname == "klipy.com" or hostname.endswith(".klipy.com"):
            return "post"
        if hostname == "tenor.com" or hostname.endswith(".tenor.com"):
            return "post"
        if hostname == "giphy.com" or hostname.endswith(".giphy.com"):
            return "post"
        return None

    async def _send_upload_error(self, ctx: Context, message: str) -> None:
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    async def _route_attachment(
        self,
        ctx: Context,
        attachment: Any,
        kind: str | None = None,
        *,
        allowed_media: set[str] | None = None,
    ) -> None:
        selected = kind
        if selected is None:
            if VideoCommands._is_video(attachment):
                selected = "video"
            elif PostCommands._is_post_attachment(attachment):
                selected = "post"
        if allowed_media is not None:
            media_type = "videos" if selected == "video" else None
            if selected == "post":
                filename = str(getattr(attachment, "filename", "")).casefold()
                media_type = "gifs" if filename.endswith(".gif") else "images"
            if media_type is not None and media_type not in allowed_media:
                return
        if selected == "video":
            await self._video_upload_impl(  # type: ignore[attr-defined]
                ctx,
                attachment=attachment,
                _allow_message_attachments=False,
                _enforce_rate_limit=False,
            )
        elif selected == "post":
            await self._post_upload_impl(  # type: ignore[attr-defined]
                ctx,
                attachment=attachment,
                _allow_message_attachments=False,
                allowed_media=allowed_media,
                _enforce_rate_limit=False,
            )
        else:
            await self._send_upload_error(
                ctx,
                "That file is not a supported video, image, or GIF.",
            )

    async def _route_url(
        self,
        ctx: Context,
        url: str,
        *,
        allowed_media: set[str] | None = None,
    ) -> None:
        source_url = normalize_download_url(url.strip().strip("<>"))
        kind = self._kind_from_url(source_url)
        if kind == "video":
            if allowed_media is not None and "videos" not in allowed_media:
                return
            await self._video_upload_impl(  # type: ignore[attr-defined]
                ctx,
                video=source_url,
                _allow_message_attachments=False,
                _enforce_rate_limit=False,
            )
            return
        if kind == "post":
            direct_suffix = Path(urlsplit(source_url).path).suffix.casefold()
            direct_media_type = (
                "gifs"
                if direct_suffix == ".gif"
                else "images" if direct_suffix in POST_EXTENSIONS else None
            )
            if allowed_media is not None and not ({"images", "gifs"} & allowed_media):
                return
            if (
                direct_media_type is not None
                and allowed_media is not None
                and direct_media_type not in allowed_media
            ):
                return
            await self._post_upload_impl(  # type: ignore[attr-defined]
                ctx,
                media=source_url,
                _allow_message_attachments=False,
                allowed_media=allowed_media,
                _enforce_rate_limit=False,
            )
            return

        # Direct URLs are intentionally not restricted to the downloader's
        # page allow-list. A user may submit a CDN URL (or a converter such as
        # gifconvert.vxtwitter.com) that is not a site page but still returns a
        # real media file. Download and classify it through the same bounded,
        # public-URL checks used by the dedicated upload commands.
        if kind is None and not is_downloadable_media_page(source_url):
            await self._route_direct_url(ctx, source_url, allowed_media=allowed_media)
            return

        # A supported page can represent either kind of media. Download it
        # once for classification, then pass the local file into the existing
        # review pipeline so it is not downloaded a second time.
        try:
            data, filename = await Downloader(
                ctx, source_url, format="mp4", hidden=True
            ).download_for_processing(
                max_bytes=_MAX_PROBE_BYTES,
                timeout=300,
            )
        except DownloadError as error:
            await self._send_upload_error(ctx, str(error))
            return

        kind = self._kind_from_bytes(data, filename)
        if kind is None:
            await self._send_upload_error(
                ctx,
                "That URL did not return a supported video, image, or GIF.",
            )
            return

        if allowed_media is not None:
            media_type = "videos" if kind == "video" else None
            if kind == "post":
                media_type = (
                    "gifs"
                    if data.startswith(_GIF_SIGNATURES)
                    or Path(filename).suffix.casefold() == ".gif"
                    else "images"
                )
            if media_type is not None and media_type not in allowed_media:
                return

        suffix = Path(filename).suffix.casefold()
        if kind == "post" and suffix not in POST_EXTENSIONS:
            suffix = ".gif" if data.startswith(_GIF_SIGNATURES) else ".png"
        elif kind == "video" and suffix not in VIDEO_EXTENSIONS:
            suffix = ".mp4"
        safe_filename = f"upload{suffix}"
        content_type = mimetypes.guess_type(safe_filename)[0] or (
            "image/gif"
            if kind == "post" and data.startswith(_GIF_SIGNATURES)
            else "image/png" if kind == "post" else "video/mp4"
        )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="fishie-upload-", suffix=suffix, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            attachment = LocalMediaAttachment(
                temporary,
                filename=safe_filename,
                source_url=source_url,
                content_type=content_type,
                size=len(data),
            )
            await self._route_attachment(
                ctx, attachment, kind, allowed_media=allowed_media
            )
        except OSError:
            await self._send_upload_error(
                ctx,
                "The media could not be prepared for upload.",
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _route_direct_url(
        self,
        ctx: Context,
        source_url: str,
        *,
        allowed_media: set[str] | None = None,
    ) -> None:
        """Fetch and route an arbitrary direct public media URL once.

        This is separate from the yt-dlp path because a direct CDN URL does
        not need to be on the downloader's supported-site list. The post
        fetch is attempted first, then the video fetch, with each path
        validating redirects, MIME types, signatures, and the 500 MB limit.
        """

        # gifconvert.vxtwitter.com returns an animated AVIF container whose
        # default Discord rendering is only the first frame. Let the post
        # resolver use the embedded source video and encode a real GIF.
        if PostCommands._gif_converter_source(source_url) is not None:
            if allowed_media is not None and "gifs" not in allowed_media:
                return
            await self._post_upload_impl(  # type: ignore[attr-defined]
                ctx,
                media=source_url,
                _allow_message_attachments=False,
                allowed_media=allowed_media,
                _enforce_rate_limit=False,
            )
            return

        temporary: Path | None = None
        try:
            try:
                download_post_url = cast(Any, self)._download_post_url
                temporary, filename, size, returned_url = cast(
                    tuple[Path, str, int, str], await download_post_url(source_url)
                )
                kind = "post"
            except commands.BadArgument as post_error:
                try:
                    temporary, filename, size, returned_url = cast(
                        tuple[Path, str, int, str],
                        await cast(Any, self)._download_video_url(source_url),
                    )
                    kind = "video"
                except commands.BadArgument as video_error:
                    # Keep the more useful media-type error when both bounded
                    # probes reject the response. Do not expose a generic
                    # unsupported-site error for a valid direct URL.
                    message = str(video_error) or str(post_error)
                    raise commands.BadArgument(message) from video_error

            if temporary is None:
                raise commands.BadArgument("The media could not be prepared.")
            suffix = Path(filename).suffix.casefold()
            content_type = mimetypes.guess_type(filename)[0] or (
                "image/gif"
                if kind == "post" and suffix == ".gif"
                else "image/png" if kind == "post" else "video/mp4"
            )
            attachment = LocalMediaAttachment(
                temporary,
                filename=filename,
                source_url=returned_url,
                content_type=content_type,
                size=size,
            )
            await self._route_attachment(
                ctx, attachment, kind, allowed_media=allowed_media
            )
        except commands.BadArgument as error:
            await self._send_upload_error(ctx, str(error))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _reply_upload_sources(
        self, ctx: Context
    ) -> tuple[list[Any], list[str], discord.Message | None]:
        replied = await self._reply_message(ctx)  # type: ignore[attr-defined]
        if replied is None:
            return [], [], None
        if self._is_library_response(replied, "video") or self._is_library_response(  # type: ignore[attr-defined]
            replied, "post"
        ):
            await ctx.send(
                "no",
                delete_after=3,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return [], [], replied
        urls: list[str] = []
        attachments = [
            candidate
            for candidate in MediaConverter._message_attachments(replied)
            if not isinstance(candidate, Mapping)
            and (
                VideoCommands._is_video(cast(Any, candidate))
                or PostCommands._is_post_attachment(cast(Any, candidate))
            )
        ]
        for candidate in MediaConverter._message_attachments(replied):
            if isinstance(candidate, Mapping):
                url = MediaConverter._attachment_url(candidate)
                if url and url not in urls:
                    urls.append(normalize_download_url(url))
        if attachments:
            return attachments, urls, replied
        try:
            url = await MediaConverter()._message_content_media_url(ctx, replied)
        except commands.BadArgument:
            url = None
        if url:
            normalized = normalize_download_url(url)
            if normalized not in urls:
                urls.append(normalized)
            return [], urls, replied
        embedded_url = MediaConverter._message_media_url(replied)
        if embedded_url:
            normalized = normalize_download_url(embedded_url)
            if normalized not in urls:
                urls.append(normalized)
        return [], urls, replied

    @cast(Any, commands.command)(name="upload")
    async def upload(self, ctx: Context, *, media: str | None = None) -> None:
        """Submit up to ten videos, images, or GIFs to their review channels."""

        lock = manual_upload_lock(ctx)
        if lock.locked():
            await ctx.send(
                "Another upload is already being processed for you. Please wait for it to finish.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        async with lock:
            await self._upload_impl(ctx, media)

    async def _upload_impl(self, ctx: Context, media: str | None = None) -> None:
        """Process a unified manual upload while the caller owns its lock."""

        async with ctx.typing():
            message = getattr(ctx, "message", None)
            attachments = [
                candidate
                for candidate in getattr(message, "attachments", ())
                if isinstance(candidate, discord.Attachment)
            ]
            if media and attachments:
                await self._send_upload_error(
                    ctx,
                    "Provide either a media URL or an attachment, not both.",
                )
                return
            if attachments:
                retry_after = consume_manual_uploads(ctx, len(attachments))
                if retry_after:
                    await self._send_upload_error(
                        ctx,
                        f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                    )
                    return
                kinds = {
                    (
                        "video"
                        if VideoCommands._is_video(candidate)
                        else (
                            "post"
                            if PostCommands._is_post_attachment(candidate)
                            else None
                        )
                    )
                    for candidate in attachments
                }
                # Let the dedicated bulk handlers submit one review message
                # per file while reporting the aggregate count once. Calling
                # the handler once is important because routing each image
                # individually would produce repeated "1 post" replies.
                if kinds == {"post"}:
                    await self._post_upload_impl(  # type: ignore[attr-defined]
                        ctx,
                        _allow_message_attachments=True,
                        _enforce_rate_limit=False,
                    )
                    return
                if kinds == {"video"}:
                    await self._video_upload_impl(  # type: ignore[attr-defined]
                        ctx,
                        _allow_message_attachments=True,
                        _enforce_rate_limit=False,
                    )
                    return
                for attachment in attachments:
                    await self._route_attachment(ctx, attachment)
                return
            if media:
                urls = MediaConverter._message_urls(media)
                if not urls:
                    urls = (media.strip(),)
                if len(urls) > 10:
                    await self._send_upload_error(
                        ctx, "You can upload at most 10 URLs at a time."
                    )
                    return
                if len(urls) > 1 and all(
                    self._kind_from_url(url) == "post" for url in urls
                ):
                    # Post uploads already have a bounded multi-URL loop and
                    # one aggregate response. Use it for an all-image/GIF
                    # batch instead of replying once per URL.
                    retry_after = consume_manual_uploads(ctx, len(urls))
                    if retry_after:
                        await self._send_upload_error(
                            ctx,
                            f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                        )
                        return
                    await self._post_upload_impl(  # type: ignore[attr-defined]
                        ctx,
                        media=" ".join(urls),
                        _allow_message_attachments=False,
                        _enforce_rate_limit=False,
                    )
                    return
                retry_after = consume_manual_uploads(ctx, len(urls))
                if retry_after:
                    await self._send_upload_error(
                        ctx,
                        f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                    )
                    return
                for url in urls:
                    await self._route_url(ctx, url)
                return

            attachments, urls, _replied = await self._reply_upload_sources(ctx)
            retry_after = consume_manual_uploads(ctx, len(attachments) + len(urls))
            if retry_after:
                await self._send_upload_error(
                    ctx,
                    f"You can upload up to 20 media files per minute. Try again in {retry_after:.0f} seconds.",
                )
                return
            if attachments:
                if all(
                    PostCommands._is_post_attachment(candidate)
                    for candidate in attachments
                ):
                    # Re-run through the post handler so a replied-to batch
                    # receives one aggregate response instead of one reply
                    # for each individual image/GIF.
                    await self._post_upload_impl(  # type: ignore[attr-defined]
                        ctx,
                        _allow_message_attachments=False,
                        _enforce_rate_limit=False,
                    )
                    return
                for attachment in attachments:
                    await self._route_attachment(ctx, attachment)
            for url in urls:
                await self._route_url(ctx, url)
            if attachments or urls:
                return
            await self._send_upload_error(
                ctx,
                "Attach or reply to a video, image, or GIF, or provide a media URL.",
            )
