"""Shared filtering and pagination for the Fishie media libraries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from utils.functions import get_or_fetch_user
from utils.paginator import LayoutPager

if TYPE_CHECKING:
    from extensions.context import Context


MEDIA_FILTER_ALIASES = {
    "gif": "gif",
    "gifs": "gif",
    "animated": "gif",
    "image": "image",
    "images": "image",
    "png": "image",
    "static": "image",
    "video": "video",
    "videos": "video",
}


@dataclass(frozen=True)
class LibraryFilters:
    """Resolved filters shared by the video, post, and combined commands."""

    user_id: int | None
    all_users: bool
    media: str | None


def split_library_filters(value: str | None) -> tuple[str | None, bool, str | None]:
    """Split dynamic media and user arguments without depending on their order.

    Unknown tokens are kept together for the Discord user converter. This
    allows a display name containing spaces while still accepting inputs such
    as ``gifs @user`` and ``@user images``.
    """

    media: str | None = None
    user_tokens: list[str] = []
    all_users = False
    for token in (value or "").split():
        normalized = token.casefold().lstrip("-")
        selected = MEDIA_FILTER_ALIASES.get(normalized)
        if selected is not None:
            if media is not None and media != selected:
                raise commands.BadArgument("Choose only one media type filter.")
            media = selected
        elif normalized == "all":
            all_users = True
        else:
            user_tokens.append(token)

    if all_users and user_tokens:
        raise commands.BadArgument("Use `all` or a Discord user, not both.")
    return (" ".join(user_tokens) or None, all_users, media)


async def resolve_library_filters(ctx: Context, value: str | None) -> LibraryFilters:
    """Resolve a dynamic filter string into a user ID and media type."""

    target, all_users, media = split_library_filters(value)
    if all_users:
        return LibraryFilters(user_id=None, all_users=True, media=media)
    if target is None:
        return LibraryFilters(user_id=ctx.author.id, all_users=False, media=media)
    try:
        user = await commands.UserConverter().convert(ctx, target)
    except commands.CommandError as error:
        raise commands.BadArgument(
            "Provide a Discord user, `all`, or a media type filter."
        ) from error
    return LibraryFilters(user_id=user.id, all_users=False, media=media)


def post_media_condition(media: str | None, *, alias: str = "p") -> str:
    """Return a static SQL condition for post image/GIF filtering."""

    if media == "gif":
        return f" AND lower({alias}.filename) LIKE '%.gif'"
    if media == "image":
        return f" AND lower({alias}.filename) NOT LIKE '%.gif'"
    if media == "video":
        raise commands.BadArgument(
            "Posts only contain images and GIFs. Use `videos` for video uploads."
        )
    return ""


def video_media_condition(media: str | None, *, alias: str = "v") -> str:
    """Return a static SQL condition for video filtering."""

    if media in {"gif", "image"}:
        raise commands.BadArgument(
            "Videos only contain videos. Use `posts` for images and GIFs."
        )
    return f" AND lower({alias}.filename) NOT LIKE '%.gif'"


class LibraryUploadsPageSource:
    """Components V2 page source for the combined media library."""

    def __init__(
        self,
        cog: Any,
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
            review_message_id = (
                int(row["review_message_id"])
                if row.get("review_message_id") is not None
                else None
            )
            if row["media_kind"] == "video":
                row["current_url"] = await self.cog._current_video_url(
                    upload_id=int(row["id"]),
                    source_url=str(row["source_url"]),
                    review_message_id=review_message_id,
                )
            else:
                row["current_url"] = await self.cog._current_post_url(
                    upload_id=int(row["id"]),
                    source_url=str(row["source_url"]),
                    review_message_id=review_message_id,
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
                discord.ui.TextDisplay(
                    "No approved uploads matching those filters were found."
                ),
            ]
        row = self.rows[page_number]
        library_id = int(row.get("library_id") or row["id"])
        uploader_id = int(row["uploader_id"])
        uploader_name = discord.utils.escape_markdown(
            str(row.get("uploader_name") or f"User {uploader_id}")
        )[:100]
        source_url = str(row.get("current_url") or row["source_url"])
        filename = str(row.get("filename") or "").casefold()
        media_label = (
            "Video"
            if row["media_kind"] == "video"
            else ("GIF" if Path(filename).suffix == ".gif" else "Image")
        )
        metadata = discord.ui.TextDisplay(
            f"-# Library ID `{library_id}` · Uploaded by {uploader_name} (`{uploader_id}`)\n"
            f"Type: {media_label}\n"
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
                    discord.ui.TextDisplay("The upload URL is currently unavailable."),
                    discord.ui.Separator(),
                    metadata,
                )
            )
        return items


async def start_library_pager(
    source: LibraryUploadsPageSource,
    *,
    ctx: Context,
    accent_color: discord.Colour | int | None = None,
) -> None:
    """Start the combined library paginator with the same pager controls."""

    await LayoutPager(
        source,
        ctx=ctx,
        accent_color=accent_color,
        timeout=600,
    ).start()
