"""Automatically submit media posted in configured upload channels.

The review libraries already contain all of the validation, conversion, and
deduplication needed by ``fish upload``.  This listener only discovers media,
enforces the per-user rate limits, and delegates each item to that shared
pipeline.  Keeping the listener thin avoids a second, subtly different upload
implementation for automatic submissions.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Deque, Mapping
from urllib.parse import urlsplit

import discord
from discord.ext import commands

from core import Cog
from utils.converters import MediaConverter
from utils.downloads import (
    DIRECT_MEDIA_HOSTS,
    is_discord_media_url,
    is_downloadable_media_page,
    normalize_download_url,
)

if TYPE_CHECKING:
    from core import Fishie
    from extensions.context import Context


UPLOAD_LOG_CHANNEL_ID = 1540539717899124797
SPAM_WINDOW_SECONDS = 30.0
BLOCK_WINDOW_SECONDS = 5 * 60.0
SPAM_THRESHOLD = 20

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".avif"})
_VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})


def _media_type(*, filename: object = "", content_type: object = "") -> str | None:
    """Return the settings key for a media filename/content type."""

    mime = str(content_type or "").split(";", 1)[0].casefold()
    suffix = Path(str(filename or "").split("?", 1)[0]).suffix.casefold()
    if mime == "image/gif" or suffix == ".gif":
        return "gifs"
    if mime.startswith("video/") or suffix in _VIDEO_SUFFIXES:
        return "videos"
    if mime.startswith("image/") or suffix in _IMAGE_SUFFIXES:
        return "images"
    return None


def _url_media_type(url: str) -> str | None:
    return _media_type(filename=urlsplit(url).path)


def _is_media_candidate_url(url: str) -> bool:
    """Accept direct media files and supported download-page URLs only."""

    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    if _url_media_type(url) is not None:
        return True
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if hostname in DIRECT_MEDIA_HOSTS or is_discord_media_url(url):
        return True
    return is_downloadable_media_page(url)


def _canonical_key(url: str) -> str:
    """Build a cheap duplicate key without retaining query-string metadata."""

    parsed = urlsplit(normalize_download_url(url))
    return parsed._replace(query="", fragment="").geturl().casefold()


class AutoUpload(Cog):
    """Handle media posted in guilds' configured auto-upload channels."""

    upload_cd_mapping = commands.CooldownMapping.from_cooldown(
        20, 60, commands.BucketType.member
    )

    @staticmethod
    def _allowed_media(bot: Fishie, channel_id: int) -> set[str]:
        configured = getattr(bot.db_cache, "auto_upload_media", {})
        values: object = (
            configured.get(channel_id) if isinstance(configured, Mapping) else None
        )
        if isinstance(values, Mapping):
            return {
                key
                for key in ("images", "gifs", "videos")
                if bool(values.get(key, True))
            }
        if isinstance(values, (set, frozenset, list, tuple)):
            return {
                key for key in ("images", "gifs", "videos") if key in values
            }
        # Existing settings and newly-created channels default to all three
        # media types.  This also keeps the event compatible while a guild's
        # settings panel is being upgraded.
        return {"images", "gifs", "videos"}

    @staticmethod
    def _collect(message: discord.Message) -> list[tuple[str, Any]]:
        """Collect direct attachments and message URLs without duplicates."""

        result: list[tuple[str, Any]] = []
        seen: set[str] = set()

        def add_url(value: object) -> None:
            if not isinstance(value, str):
                return
            try:
                url = normalize_download_url(value.strip().strip("<>").rstrip(
                    ".,!?;:'\"`]}",
                ))
            except (TypeError, ValueError):
                return
            if not _is_media_candidate_url(url):
                return
            key = _canonical_key(url)
            if key in seen:
                return
            seen.add(key)
            result.append(("url", url))

        # Actual Discord attachments are passed through the upload pipeline
        # as attachments, preserving the original file instead of downloading
        # it a second time.
        for attachment in getattr(message, "attachments", ()) or ():
            url = MediaConverter._attachment_url(attachment)
            if not url:
                continue
            key = _canonical_key(url)
            if key in seen:
                continue
            seen.add(key)
            result.append(("attachment", attachment))

        # Include forwarded-message snapshots, text links, and embed media.
        # MediaConverter already normalizes all of those Discord payload forms.
        snapshots = MediaConverter._message_snapshots(message)
        for source in snapshots:
            attachments = MediaConverter._message_sequence(
                MediaConverter._message_value(source, "attachments", ())
            )
            for attachment in attachments:
                add_url(MediaConverter._attachment_url(attachment))

        content_start = len(result)
        for source in (message, *snapshots):
            content = MediaConverter._message_value(source, "content", "")
            if isinstance(content, str):
                for url in MediaConverter._message_urls(content):
                    add_url(url)
        # A normal Discord link may expose both the page URL in ``content``
        # and a CDN preview in ``embeds``.  The page is the canonical source,
        # so do not submit its preview a second time.  Embed-only messages are
        # still supported when no media URL was written in the content.
        if len(result) == content_start:
            for url in MediaConverter._message_media_urls(message):
                add_url(url)
        return result

    def _history(self) -> dict[tuple[int, int], Deque[tuple[float, int]]]:
        history = getattr(self, "_auto_upload_history", None)
        if history is None:
            history = defaultdict(deque)
            self._auto_upload_history = history
        return history

    def _blocked(self) -> dict[tuple[int, int], float]:
        blocked = getattr(self, "_auto_upload_blocked", None)
        if blocked is None:
            blocked = {}
            self._auto_upload_blocked = blocked
        return blocked

    def _record_attempts(
        self, key: tuple[int, int], count: int, now: float
    ) -> tuple[int, int]:
        history = self._history()[key]
        history.append((now, count))
        cutoff = now - BLOCK_WINDOW_SECONDS
        while history and history[0][0] < cutoff:
            history.popleft()
        recent_30 = sum(
            amount
            for stamp, amount in history
            if stamp >= now - SPAM_WINDOW_SECONDS
        )
        recent_300 = sum(amount for _stamp, amount in history)
        return recent_30, recent_300

    async def _upload_log(
        self,
        message: discord.Message,
        *,
        count_30: int,
        count_300: int,
    ) -> None:
        channel = self.bot.get_channel(UPLOAD_LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(UPLOAD_LOG_CHANNEL_ID)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                self.bot.logger.warning(
                    "Could not find the auto-upload log channel %s",
                    UPLOAD_LOG_CHANNEL_ID,
                )
                return
        if not isinstance(channel, discord.abc.Messageable):
            return
        guild_id = message.guild.id if message.guild is not None else None
        author = discord.utils.escape_markdown(str(message.author))
        text = (
            "## Auto-upload spam block\n"
            f"**User:** {author} (`{message.author.id}`)\n"
            f"**Guild:** `{guild_id}`\n"
            f"**Uploads:** {count_30} media in the last 30 seconds · "
            f"{count_300} in the last 5 minutes\n"
            "**Block duration:** 5 minutes"
        )
        try:
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            self.bot.logger.warning(
                "Could not send auto-upload spam log", exc_info=True
            )

    async def _warn(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(
                text,
                mention_author=False,
                delete_after=10,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            self.bot.logger.debug("Could not warn auto-upload user", exc_info=True)

    async def _handle(self, message: discord.Message) -> None:
        if message.author.bot or message.channel.id not in getattr(
            self.bot.db_cache, "auto_uploads", set()
        ):
            return
        candidates = self._collect(message)
        if not candidates:
            return
        allowed = self._allowed_media(self.bot, message.channel.id)
        if not allowed:
            return
        filtered: list[tuple[str, Any]] = []
        for kind, value in candidates:
            if kind == "attachment":
                media_type = _media_type(
                    filename=getattr(value, "filename", ""),
                    content_type=getattr(value, "content_type", ""),
                )
                if media_type is not None and media_type not in allowed:
                    continue
            else:
                media_type = _url_media_type(str(value))
                if media_type is not None and media_type not in allowed:
                    continue
            filtered.append((kind, value))
        if not filtered:
            return

        key = (message.guild.id if message.guild is not None else 0, message.author.id)
        now = time.monotonic()
        blocked = self._blocked()
        blocked_until = blocked.get(key, 0.0)
        if blocked_until > now:
            return
        blocked.pop(key, None)
        count_30, count_300 = self._record_attempts(key, len(filtered), now)
        if count_30 >= SPAM_THRESHOLD:
            blocked[key] = now + BLOCK_WINDOW_SECONDS
            await self._warn(
                message,
                "You are uploading media too quickly. Auto-uploads are paused "
                "for you for 5 minutes.",
            )
            await self._upload_log(
                message, count_30=count_30, count_300=count_300
            )
            return

        bucket = self.upload_cd_mapping.get_bucket(message)
        retry_after = None
        if bucket is not None:
            for _ in filtered:
                retry_after = bucket.update_rate_limit()
                if retry_after:
                    break
        if retry_after:
            await self._warn(
                message,
                "You can upload up to 20 media files per minute. Please slow down.",
            )
            return

        fun = self.bot.get_cog("Fun")
        if fun is None:
            return
        ctx: Context = await self.bot.get_context(message)  # type: ignore[assignment]
        for kind, value in filtered:
            try:
                if kind == "attachment":
                    await fun._route_attachment(  # type: ignore[attr-defined]
                        ctx,
                        value,
                        allowed_media=allowed,
                    )
                else:
                    await fun._route_url(  # type: ignore[attr-defined]
                        ctx,
                        str(value),
                        allowed_media=allowed,
                    )
            except commands.CommandError as error:
                self.bot.logger.info(
                    "Auto-upload skipped media from %s: %s", message.id, error
                )
            except (discord.HTTPException, OSError):
                self.bot.logger.exception(
                    "Auto-upload failed for message %s", message.id
                )

    @commands.Cog.listener("on_message")
    async def auto_upload(self, message: discord.Message) -> None:
        try:
            await self._handle(message)
        except Exception:
            self.bot.logger.exception(
                "Auto-upload listener failed for message %s", message.id
            )
