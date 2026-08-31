"""Small helpers for resolving cached profile colours.

Profile colours are loaded by :class:`core.cache.db_cache` at startup.  This
module deliberately never queries the database; callers pass an object that
exposes the synchronous ``get_user_color`` cache accessor.
"""

from __future__ import annotations

from typing import Any

import discord


def color_value(value: Any) -> int | None:
    """Convert a Discord colour, cache row, integer, or hex string to RGB."""

    if isinstance(value, discord.Colour):
        return value.value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 0xFFFFFF else None
    if isinstance(value, dict):
        return color_value(value.get("hex_value") or value.get("color_value"))
    if isinstance(value, str):
        text = value.strip().lower()
        if text.startswith("#"):
            text = text[1:]
        elif text.startswith("0x"):
            text = text[2:]
        if len(text) != 6:
            return None
        try:
            parsed = int(text, 16)
        except ValueError:
            return None
        return parsed if 0 <= parsed <= 0xFFFFFF else None
    return None


def get_cached_user_color(
    source: Any, user_id: int | None, default: int | discord.Colour
) -> discord.Colour:
    """Return a user's cached colour, falling back without database access."""

    fallback = color_value(default)
    if fallback is None:
        fallback = discord.Colour.blurple().value
    if user_id is None:
        return discord.Colour(fallback)
    getter = getattr(source, "get_user_color", None)
    if not callable(getter):
        return discord.Colour(fallback)
    try:
        resolved = color_value(getter(int(user_id)))
    except (AttributeError, TypeError, ValueError):
        resolved = None
    return discord.Colour(fallback if resolved is None else resolved)
