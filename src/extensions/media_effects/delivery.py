"""Discord delivery and temporary-hosting helpers for media effects."""

from __future__ import annotations

import mimetypes
import time
from io import BytesIO
from typing import TYPE_CHECKING, Any

import discord
from discord import MediaGalleryItem, ui

from utils import TemporaryMediaError, upload_temporary_media

from .processing import EffectResult, compress_media_to_size

if TYPE_CHECKING:
    from core.bot import Fishie
    from extensions.context import Context


def _upload_limit(ctx: Context) -> int:
    if ctx.guild is not None:
        return ctx.guild.filesize_limit
    return discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES


def _safe_filename(filename: str) -> str:
    return filename.replace("/", "_").replace("\\", "_")


async def send_effect_result(
    bot: Fishie,
    ctx: Context,
    result: EffectResult,
    *,
    started: float,
    note: str = "",
) -> None:
    """Deliver one processed file, hosting only when Discord cannot accept it."""

    max_size = _upload_limit(ctx)
    compressed = False
    if len(result.data) > max_size:
        original_result = result
        try:
            result = await compress_media_to_size(
                result.data,
                result.filename,
                max_size,
            )
        except ValueError:
            result = original_result
        compressed = result.data != original_result.data

    info_lines = [
        f"-# Invoked by {ctx.author.mention}",
        f"-# Took {time.monotonic() - started:.1f}s",
    ]
    if note:
        info_lines.append(f"-# {note}")
    if compressed:
        info_lines.append("-# Compressed to fit the server upload limit")

    filename = _safe_filename(result.filename)
    hosted_url: str | None = None
    if len(result.data) > max_size:
        try:
            hosted_url = await upload_temporary_media(
                bot,
                result.data,
                filename,
                content_type=mimetypes.guess_type(filename)[0],
            )
        except TemporaryMediaError as error:
            raise ValueError(
                "The result is too large for Discord and could not be hosted "
                "temporarily. Try fewer effects, a shorter video, or a smaller source."
            ) from error
        info_lines.append(
            "-# Discord's upload limit was exceeded. This link expires in 30 minutes."
        )

    info_text = "\n".join(info_lines)
    if hosted_url is not None:
        container_items: list[ui.Item[Any]]
        if result.displayable:
            container_items = [
                ui.MediaGallery(MediaGalleryItem(hosted_url)),
                ui.TextDisplay(info_text),
            ]
        else:
            container_items = [
                ui.TextDisplay(f"[Open the generated file]({hosted_url})\n{info_text}")
            ]
        container = ui.Container(*container_items, accent_color=bot.embedcolor)
        view = ui.LayoutView(timeout=None)
        view.add_item(container)
        await ctx.send(
            view=view,
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        return

    if not result.displayable:
        await ctx.send(
            content=info_text,
            file=discord.File(BytesIO(result.data), filename),
            reference=ctx.message.to_reference(fail_if_not_exists=False),
        )
        return

    container = ui.Container(
        ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}")),
        ui.TextDisplay(info_text),
        accent_color=bot.embedcolor,
    )
    view = ui.LayoutView(timeout=None)
    view.add_item(container)
    await ctx.send(
        file=discord.File(BytesIO(result.data), filename),
        view=view,
        reference=ctx.message.to_reference(fail_if_not_exists=False),
    )


async def send_effect_results(
    bot: Fishie,
    ctx: Context,
    results: list[EffectResult],
    *,
    started: float,
) -> None:
    """Deliver several converted files with one consistent CV2 footer."""

    if not results:
        raise ValueError("No files were converted.")
    max_size = _upload_limit(ctx)
    fitted_results: list[EffectResult] = []
    for result in results:
        if len(result.data) > max_size:
            try:
                result = await compress_media_to_size(
                    result.data,
                    result.filename,
                    max_size,
                )
            except ValueError:
                pass
        fitted_results.append(result)
    results = fitted_results

    hosted_urls: dict[int, str] = {}
    for index, result in enumerate(results):
        if len(result.data) <= max_size:
            continue
        filename = _safe_filename(result.filename)
        try:
            hosted_urls[index] = await upload_temporary_media(
                bot,
                result.data,
                filename,
                content_type=mimetypes.guess_type(filename)[0],
            )
        except TemporaryMediaError as error:
            raise ValueError(
                "At least one converted file is too large for Discord and "
                "could not be hosted temporarily."
            ) from error

    filenames = [_safe_filename(result.filename) for result in results]
    items: list[ui.Item[Any]] = []
    files: list[discord.File] = []
    for index, (result, filename) in enumerate(zip(results, filenames)):
        hosted_url = hosted_urls.get(index)
        if hosted_url is not None and result.displayable:
            items.append(ui.MediaGallery(MediaGalleryItem(hosted_url)))
        elif result.displayable:
            items.append(ui.MediaGallery(MediaGalleryItem(f"attachment://{filename}")))
            files.append(discord.File(BytesIO(result.data), filename))
        elif hosted_url is not None:
            items.append(ui.TextDisplay(f"[Open the generated file]({hosted_url})"))
        else:
            items.append(ui.File(f"attachment://{filename}"))
            files.append(discord.File(BytesIO(result.data), filename))

    footer_lines = [
        f"-# Invoked by {ctx.author.mention}",
        f"-# Took {time.monotonic() - started:.1f}s",
    ]
    if hosted_urls:
        footer_lines.append("-# Oversized files are hosted for 30 minutes")
    items.append(ui.TextDisplay("\n".join(footer_lines)))

    container = ui.Container(*items, accent_color=bot.embedcolor)
    view = ui.LayoutView(timeout=None)
    view.add_item(container)
    await ctx.send(
        files=files,
        view=view,
        reference=ctx.message.to_reference(fail_if_not_exists=False),
    )
