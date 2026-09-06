"""Helpers for sending tracked images through the configured webhooks."""

from __future__ import annotations

import random
from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from core import Fishie


def _image_webhook_urls(bot: Fishie) -> tuple[str, ...]:
    """Return the configured image webhooks without duplicate/blank entries."""

    configured = bot.config.get("webhooks", {}).get("images", ())
    return tuple(
        dict.fromkeys(str(url).strip() for url in configured if str(url).strip())
    )


async def send_tracked_image(
    bot: Fishie,
    content: str,
    image: BytesIO,
    filename: str,
) -> discord.Message:
    """Send an image, rotating past deleted image webhooks when necessary.

    Image-history webhooks are a shared static pool.  A deleted webhook should
    not make an avatar or icon update fail, so each configured URL is tried at
    most once in a randomized order.  A fresh ``BytesIO`` is used for every
    attempt because Discord consumes the file object during upload.
    """

    urls = _image_webhook_urls(bot)
    if not urls:
        raise commands.BadArgument("No image logging webhooks are configured.")

    payload = image.getvalue()
    start = random.randrange(len(urls))
    last_not_found: discord.NotFound | None = None

    for offset in range(len(urls)):
        index = (start + offset) % len(urls)
        url = urls[index]
        try:
            webhook = discord.Webhook.from_url(url, session=bot.session)
            return await webhook.send(
                content,
                file=discord.File(BytesIO(payload), filename=filename),
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound as error:
            # Discord uses 10015 (Unknown Webhook) for a deleted webhook.  A
            # 404 from this endpoint is not recoverable for this URL, so move
            # on to the next configured webhook without exposing its token.
            last_not_found = error
            bot.logger.warning(
                "Image logging webhook %d/%d is unavailable; trying another",
                index + 1,
                len(urls),
            )
        except ValueError:
            # Ignore malformed legacy entries and continue with the pool.
            bot.logger.warning(
                "Image logging webhook %d/%d has an invalid URL; trying another",
                index + 1,
                len(urls),
            )

    if last_not_found is not None:
        raise last_not_found
    raise commands.BadArgument("No valid image logging webhooks are available.")


__all__ = ["send_tracked_image"]
