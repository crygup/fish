"""Runtime identity helpers shared by background event handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bot import Fishie


def is_legacy_instance(bot: Fishie) -> bool:
    """Return whether *bot* is running the inactive legacy application."""

    return bot.is_legacy_bot
