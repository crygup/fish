from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from extensions.discord_ext.info import UPLOADER_ROLE_ID, Info
from utils.vars import load_user_badges_document, render_user_badge


@pytest.mark.asyncio
async def test_uploader_badge_follows_review_server_role() -> None:
    member = SimpleNamespace(roles=[SimpleNamespace(id=UPLOADER_ROLE_ID)])
    guild = SimpleNamespace(
        chunked=True,
        get_member=lambda user_id: member if user_id == 123 else None,
    )
    cog = object.__new__(Info)
    cast(Any, cog).bot = SimpleNamespace(get_guild=lambda _guild_id: guild)

    assert await cog.has_uploader_badge(123) is True
    assert await cog.has_uploader_badge(456) is False


@pytest.mark.asyncio
async def test_inactive_badge_is_not_restored_from_startup_cache() -> None:
    """Selling a badge must take effect before the next bot restart."""

    user = SimpleNamespace(id=123, public_flags={})
    stale_badge = {
        "badge_key": "purchase:fish",
        "emoji_name": "🐟",
        "text": "fish",
        "active": True,
    }
    bot = SimpleNamespace(
        is_owner=AsyncMock(return_value=False),
        db_cache=SimpleNamespace(user_badges={123: [stale_badge]}),
        badges=SimpleNamespace(owned=AsyncMock(return_value=[])),
        pool=SimpleNamespace(fetchrow=AsyncMock(return_value=None)),
    )
    cog = object.__new__(Info)
    cog.bot = bot
    cog.has_uploader_badge = AsyncMock(return_value=False)

    entries = await cog.get_badge_entries(user, SimpleNamespace())

    assert entries == []


def test_uploader_badge_catalog_entry() -> None:
    entry = load_user_badges_document()["flags"]["uploader"]

    assert render_user_badge(entry) == "📤 Uploader"
