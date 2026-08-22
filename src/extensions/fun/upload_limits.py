"""Shared safeguards for user-triggered media-library uploads.

Automatic upload channels have their own listener-level limits.  These guards
cover the command paths (``video upload``, ``post upload``, and ``upload``)
without being applied when those paths are called by the automatic listener.
"""

from __future__ import annotations

import asyncio
from collections.abc import Hashable
from typing import Any

from discord.ext import commands

MANUAL_UPLOAD_RATE = 20
MANUAL_UPLOAD_PERIOD = 60.0

# Keep one mapping for all manual upload commands so a user cannot bypass the
# limit by alternating between ``upload``, ``video upload``, and ``post upload``.
manual_upload_cooldown = commands.CooldownMapping.from_cooldown(
    MANUAL_UPLOAD_RATE,
    MANUAL_UPLOAD_PERIOD,
    commands.BucketType.member,
)
_manual_upload_locks: dict[Hashable, asyncio.Lock] = {}


def _upload_key(ctx: Any) -> tuple[int, int]:
    guild = getattr(ctx, "guild", None)
    author = getattr(ctx, "author", None)
    return (int(getattr(guild, "id", 0) or 0), int(getattr(author, "id", 0) or 0))


def consume_manual_uploads(ctx: Any, count: int = 1) -> float | None:
    """Reserve ``count`` upload slots and return seconds to wait if limited."""

    if count <= 0:
        return None
    return manual_upload_cooldown.update_rate_limit(ctx, tokens=count)


def manual_upload_lock(ctx: Any) -> asyncio.Lock:
    """Return the per-user lock used to prevent concurrent large uploads."""

    key = _upload_key(ctx)
    lock = _manual_upload_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _manual_upload_locks[key] = lock
    return lock


def reset_manual_upload_limits() -> None:
    """Clear in-memory safeguards for tests and controlled process reloads."""

    manual_upload_cooldown._cache.clear()
    _manual_upload_locks.clear()
