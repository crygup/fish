from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import discord
import pytest

from extensions.search.steam import (
    Steam,
    _steam_format_review_summary,
    _steam_free_to_keep_games,
    _steam_store_review_summary,
)


class _Response:
    status = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, **_kwargs: object) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.calls.append(url)
        if "/api/storesearch/" in url:
            return _Response({"items": [{"id": 620, "name": "Portal 2"}]})
        if "/api/appdetails/" in url:
            return _Response({"620": {"success": True, "data": {"steam_appid": 620}}})
        if "day_range=30" in url:
            return _Response(
                {
                    "query_summary": {
                        "total_reviews": 25,
                        "total_positive": 20,
                        "review_score_desc": "Very Positive",
                    }
                }
            )
        return _Response(
            {
                "query_summary": {
                    "total_reviews": 10_000,
                    "total_positive": 8_000,
                    "review_score_desc": "Very Positive",
                }
            }
        )


def test_steam_reviews_show_only_the_overall_summary() -> None:
    text = _steam_format_review_summary(
        {
            "query_summary": {
                "total_reviews": 10_000,
                "total_positive": 8_000,
                "review_score_desc": "Very Positive",
            },
        }
    )
    assert text == "**Reviews:** Very Positive · 80.0% (8,000 positive / 10,000 total)"
    assert "Recent" not in text


def test_steam_store_review_summary_replaces_language_bucket_counts() -> None:
    text = _steam_format_review_summary(
        {
            "query_summary": {
                "total_reviews": 7,
                "total_positive": 5,
                "review_score_desc": "7 user reviews",
            },
            "_store_summary": {
                "total_reviews": 4_636,
                "total_positive": 4_210,
                "review_score_desc": "Very Positive",
            },
        }
    )
    assert text == "**Reviews:** Very Positive · 90.8% (4,210 positive / 4,636 total)"


def test_steam_store_page_and_free_search_parsers() -> None:
    review_html = (
        '<input id="review_summary_num_positive_reviews" value="4210">'
        '<input id="review_summary_num_reviews" value="4636">'
        '<span class="game_review_summary positive">Very Positive</span>'
    )
    assert _steam_store_review_summary(review_html) == {
        "total_reviews": 4_636,
        "total_positive": 4_210,
        "review_score_desc": "Very Positive",
    }
    free_html = (
        '<a class="search_result_row" data-ds-appid="620">'
        '<span class="title">Portal 2</span>'
        '<div class="discount_block" data-price-final="0">'
        '<div class="discount_original_price">$9.99</div>'
        "</div></a>"
    )
    assert _steam_free_to_keep_games(free_html) == [
        "[Portal 2](https://store.steampowered.com/app/620/) · ~~$9.99~~ → **Free**"
    ]


@pytest.mark.asyncio
async def test_steam_game_requests_the_overall_review_summary() -> None:
    session = _Session()
    cog = cast(Any, object.__new__(Steam))
    cog.bot = SimpleNamespace(session=session)
    game, reviews = await cog._get_game("Portal 2")
    assert game is not None and reviews is not None
    review_calls = [call for call in session.calls if "/appreviews/" in call]
    assert len(review_calls) == 1
    review_query = parse_qs(urlparse(review_calls[0]).query)
    assert review_query["filter"] == ["all"]
    assert review_query["num_per_page"] == ["1"]
    assert "day_range" not in review_query


def test_steam_game_view_preserves_current_components_order() -> None:
    cog = cast(Any, object.__new__(Steam))
    cog.bot = SimpleNamespace(embedcolor=discord.Colour.blurple())
    view = cog._game_view(
        {
            "steam_appid": 620,
            "name": "Portal 2",
            "short_description": "A puzzle game.",
            "genres": [{"description": "Action"}],
            "developers": ["Valve"],
            "publishers": ["Valve"],
            "website": "https://www.thinkwithportals.com/",
            "release_date": {"date": "Apr 18, 2011"},
            "header_image": "https://cdn.akamai.steamstatic.com/steam/apps/620/header.jpg",
        },
        {},
    )
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    assert isinstance(container.children[0], discord.ui.TextDisplay)
    assert isinstance(container.children[1], discord.ui.TextDisplay)
    assert isinstance(container.children[2], discord.ui.Separator)
    assert isinstance(container.children[3], discord.ui.TextDisplay)
    assert isinstance(container.children[4], discord.ui.Separator)
    assert isinstance(container.children[5], discord.ui.MediaGallery)
    assert isinstance(container.children[6], discord.ui.TextDisplay)
    metadata = container.children[3]
    assert isinstance(metadata, discord.ui.TextDisplay)
    assert "**Developer:** Valve" in metadata.content
    assert "**Publisher:** Valve" in metadata.content
    assert "**Links:**" not in metadata.content
    assert isinstance(view.children[1], discord.ui.ActionRow)
    assert [button.label for button in view.children[1].children] == [
        "Steam store",
        "Official website",
    ]
