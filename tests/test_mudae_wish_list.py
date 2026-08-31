from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from extensions.mudae import Mudae, _MudaeWishes
from extensions.mudae.wish_list import SeriesWishEntry, SeriesWishListView


@pytest.mark.asyncio
async def test_series_wishes_mark_unscraped_names_italic() -> None:
    pool = SimpleNamespace(
        fetch=AsyncMock(
            side_effect=(
                [
                    {"id": 12, "wish_type": "series", "wish_value": "saved show"},
                    {
                        "id": 13,
                        "wish_type": "series",
                        "wish_value": "brand new show",
                    },
                ],
                [{"normalized_name": "saved show", "series_name": "Saved Show"}],
            )
        )
    )
    bot = SimpleNamespace(pool=pool, logger=SimpleNamespace(debug=lambda *args: None))
    cog = Mudae.__new__(Mudae)
    cast(Any, cog).bot = bot
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        author=SimpleNamespace(id=456, name="Tester"),
    )

    entries = await cog._series_wish_entries(ctx, _MudaeWishes())

    assert entries == (
        SeriesWishEntry(id=12, name="Saved Show", exact=True),
        SeriesWishEntry(id=13, name="brand new show", exact=False),
    )
    assert SeriesWishListView._entry_text(entries[0]) == "`12` · **Saved Show**"
    assert SeriesWishListView._entry_text(entries[1]) == ("`13` · ***brand new show***")


def test_series_wish_list_view_has_ten_entry_pages_and_looping_controls() -> None:
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=456, name="Tester"),
        bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
    )
    entries = tuple(
        SeriesWishEntry(id=index, name=f"Series {index}", exact=index % 2 == 0)
        for index in range(11)
    )

    view = SeriesWishListView(ctx, entries)

    assert view.page_count == 2
    assert [button.label for button in view._buttons] == ["<", ">"]
    assert not any(button.disabled for button in view._buttons)
    assert len(view.children) == 2


def test_series_wish_list_view_empty_page_keeps_requested_heading() -> None:
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=456, name="Tester"),
        bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
    )
    view = SeriesWishListView(ctx, ())

    assert view.page_count == 1
    assert view.entries == ()
