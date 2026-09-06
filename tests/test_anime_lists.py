from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord
from test_support import require_type

from core import Fishie
from extensions.anime import (
    ANILIST_CURRENT_LIST_PUBLIC_QUERY,
    ANILIST_CURRENT_LIST_QUERY,
    ANILIST_RECENT_ACTIVITY_QUERY,
    ANILIST_RECENT_RATINGS_QUERY,
    ANILIST_USER_QUERY,
    AniListRecentView,
    Anime,
    MediaListView,
    _recent_activity_line,
    _recent_progress_text,
)
from extensions.context import Context


def _context() -> Context:
    return cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )


def _entry(index: int, *, repeat: int = 0, score: int | None = 8) -> dict[str, Any]:
    return {
        "id": index,
        "mediaId": 1000 + index,
        "status": "CURRENT",
        "progress": index,
        "score": score,
        "repeat": repeat,
        "media": {
            "id": 1000 + index,
            "siteUrl": f"https://anilist.co/anime/{1000 + index}",
            "title": {"userPreferred": f"Title {index}"},
            "episodes": 12,
        },
    }


def test_active_list_view_labels_anilist_repeating_status() -> None:
    entry = _entry(0)
    entry["status"] = "REPEATING"
    view = MediaListView(
        cast(Any, SimpleNamespace()),
        _context(),
        [entry],
        "manga",
        "token",
        "POINT_100",
        "fluttershy",
    )
    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "Rereading" in text


def test_active_list_query_requests_current_entries_and_progress_fields() -> None:
    assert "MediaListCollection" in ANILIST_CURRENT_LIST_QUERY
    assert "status_in: $statuses" in ANILIST_CURRENT_LIST_QUERY
    assert "repeat" in ANILIST_CURRENT_LIST_QUERY
    assert "media {" in ANILIST_CURRENT_LIST_QUERY
    assert "Viewer" in ANILIST_CURRENT_LIST_QUERY
    assert "Viewer" not in ANILIST_CURRENT_LIST_PUBLIC_QUERY


def test_watching_and_reading_are_standalone_commands() -> None:
    assert Anime.watching.name == "watching"
    assert Anime.reading.name == "reading"
    assert Anime.watching.app_command is not None
    assert Anime.reading.app_command is not None
    watching_parameter = Anime.watching.app_command.get_parameter("user")
    reading_parameter = Anime.reading.app_command.get_parameter("user")
    assert watching_parameter is not None and watching_parameter.required is False
    assert reading_parameter is not None and reading_parameter.required is False


def test_active_list_view_paginates_and_displays_repeat_and_score() -> None:
    view = MediaListView(
        cast(Any, SimpleNamespace()),
        _context(),
        [_entry(index, repeat=1 if index == 0 else 0) for index in range(11)],
        "anime",
        "token",
        "POINT_10",
        "fluttershy",
    )

    assert view.page_count == 3
    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "Rewatching" in text
    assert "Score: 8/10" in text
    assert "Currently watching for fluttershy" in text
    assert "### Title 0" not in text
    first_separator = next(
        index
        for index, child in enumerate(container.children)
        if isinstance(child, discord.ui.Separator)
    )
    selected_details = container.children[first_separator + 1]
    assert isinstance(selected_details, discord.ui.TextDisplay)
    assert "Score:" not in selected_details.content
    select_row = container.children[first_separator + 2]
    assert isinstance(select_row, discord.ui.ActionRow)
    assert isinstance(select_row.children[0], discord.ui.Select)
    assert any(
        isinstance(child, discord.ui.ActionRow)
        and any(
            isinstance(button, discord.ui.Button) and button.label in {"<", ">"}
            for button in child.children
        )
        for child in container.children
    )
    assert any(
        isinstance(child, discord.ui.TextDisplay)
        and "Page 1/3" in child.content
        and "11 active anime titles" in child.content
        for child in container.children
    )


def test_active_list_view_handles_empty_lists() -> None:
    view = MediaListView(
        cast(Any, SimpleNamespace()),
        _context(),
        [],
        "manga",
        "token",
        "POINT_100",
        "fluttershy",
    )
    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "not currently reading any manga titles" in text


def test_public_list_hides_progress_controls_and_counts() -> None:
    view = MediaListView(
        cast(Any, SimpleNamespace()),
        _context(),
        [_entry(0)],
        "anime",
        None,
        None,
        "fluttershy",
    )
    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "Score: 8/100" in text
    assert "0/12 episodes" in text
    assert "Progress:" not in text
    assert not any(
        isinstance(child, discord.ui.ActionRow)
        and any(isinstance(item, discord.ui.Select) for item in child.children)
        for child in container.children
    )


def test_recent_activity_queries_include_all_supported_activity_types() -> None:
    assert "User(name: $name)" in ANILIST_USER_QUERY
    assert "activities(userId: $userId, sort: ID_DESC)" in ANILIST_RECENT_ACTIVITY_QUERY
    assert "... on ListActivity" in ANILIST_RECENT_ACTIVITY_QUERY
    assert "... on TextActivity" in ANILIST_RECENT_ACTIVITY_QUERY
    assert "... on MessageActivity" in ANILIST_RECENT_ACTIVITY_QUERY
    assert "$perPage" in ANILIST_RECENT_ACTIVITY_QUERY
    assert "mediaListEntry { score }" in ANILIST_RECENT_ACTIVITY_QUERY
    assert "mediaList(userId: $userId, mediaId_in: $mediaIds)" in (
        ANILIST_RECENT_RATINGS_QUERY
    )
    assert "mediaListOptions { scoreFormat }" in ANILIST_RECENT_RATINGS_QUERY


def test_recent_activity_view_shows_five_entries_and_external_pagination() -> None:
    entries = [
        {
            "__typename": "ListActivity",
            "createdAt": index + 1,
            "status": "watched",
            "progress": f"Episode {index + 1}",
            "siteUrl": f"https://anilist.co/activity/{index + 1}",
            "media": {
                "siteUrl": f"https://anilist.co/anime/{index + 1}",
                "title": {"userPreferred": f"Title {index + 1}"},
            },
        }
        for index in range(6)
    ]
    view = AniListRecentView(
        cast(Any, SimpleNamespace()),
        _context(),
        user_id=42,
        username="Ani User",
        access_token=None,
        entries=entries,
        page_count=2,
    )

    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "Recent AniList activity for Ani User" in text
    assert "Title 1" in text and "Title 5" in text
    assert "Title 6" not in text
    assert "Page 1/2" in text
    navigation = cast(discord.ui.ActionRow, view.children[1])
    assert [
        require_type(button, discord.ui.Button).label for button in navigation.children
    ] == ["<", ">"]
    assert all(
        not require_type(button, discord.ui.Button).disabled
        for button in navigation.children
    )


def test_recent_activity_line_formats_text_and_escapes_mentions() -> None:
    line = _recent_activity_line(
        {
            "__typename": "TextActivity",
            "createdAt": 123,
            "text": "hello @everyone",
            "siteUrl": "https://anilist.co/activity/123",
        }
    )
    assert "Posted: hello @\u200beveryone" in line
    assert "<t:123:R>" in line


def test_recent_activity_line_uses_activity_link_and_optional_rating() -> None:
    line = _recent_activity_line(
        {
            "__typename": "ListActivity",
            "createdAt": 123,
            "status": "completed",
            "rating": "8/10",
            "siteUrl": "https://anilist.co/activity/123",
            "media": {
                "siteUrl": "https://anilist.co/anime/123",
                "title": {"userPreferred": "Title"},
            },
        }
    )
    assert "Completed [Title](https://anilist.co/activity/123)" in line
    assert "8/10 · <t:123:R>" in line
    assert "anilist.co/anime/123" not in line


def test_recent_activity_line_formats_anilist_progress_status_and_media_rating() -> (
    None
):
    line = _recent_activity_line(
        {
            "__typename": "ListActivity",
            "createdAt": 123,
            "status": "watched episode",
            "progress": "1 - 2",
            "siteUrl": "https://anilist.co/activity/123",
            "media": {
                "type": "ANIME",
                "title": {"userPreferred": "Title"},
                "mediaListEntry": {"score": 8, "scoreFormat": "POINT_10"},
            },
        }
    )
    assert "Watched Episodes 1 - 2 for [Title]" in line
    assert "\n-# 8/10 · <t:123:R>" in line


def test_recent_activity_progress_pluralises_ranges_and_single_entries() -> None:
    anime = {"media": {"type": "ANIME"}}
    assert _recent_progress_text("Episode 1", anime) == "Episode 1"
    assert _recent_progress_text("5 - 11", anime) == "Episodes 5 - 11"
    manga = {"media": {"type": "MANGA"}}
    assert _recent_progress_text("Chapter 4", manga) == "Chapter 4"
    assert _recent_progress_text("4-5", manga) == "Chapters 4 - 5"


def test_recent_activity_rating_is_kept_when_supplied_by_the_list_lookup() -> None:
    line = _recent_activity_line(
        {
            "__typename": "ListActivity",
            "createdAt": 123,
            "status": "completed",
            "rating": 8,
            "siteUrl": "https://anilist.co/activity/123",
            "media": {
                "type": "ANIME",
                "title": {"userPreferred": "Title"},
            },
        }
    )
    assert "Completed [Title](https://anilist.co/activity/123)" in line
    assert "\n-# 8 · <t:123:R>" in line

    formatted_line = _recent_activity_line(
        {
            "__typename": "ListActivity",
            "createdAt": 123,
            "status": "completed",
            "rating": 8,
            "_scoreFormat": "POINT_10",
            "siteUrl": "https://anilist.co/activity/123",
            "media": {"title": {"userPreferred": "Title"}},
        }
    )
    assert "\n-# 8/10 · <t:123:R>" in formatted_line


def test_recent_activity_view_keeps_custom_color_when_rerendered() -> None:
    custom_color = discord.Colour(0xABCDEF)
    context = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
            embedcolor=custom_color.value,
        ),
    )
    view = AniListRecentView(
        cast(Any, SimpleNamespace()),
        context,
        user_id=42,
        username="Ani User",
        access_token=None,
        entries=[],
        page_count=2,
    )
    first_container = cast(discord.ui.Container, view.children[0])
    assert first_container.accent_color == custom_color.value
    view.page = 2
    view._render()
    second_container = cast(discord.ui.Container, view.children[0])
    assert second_container.accent_color == custom_color.value


async def test_recent_activity_fetch_attaches_public_list_ratings() -> None:
    cog = cast(Anime, object.__new__(Anime))
    cog.bot = cast(
        "Fishie", SimpleNamespace(logger=SimpleNamespace(debug=lambda *args: None))
    )
    responses = [
        (
            200,
            {
                "data": {
                    "Page": {
                        "activities": [
                            {
                                "__typename": "ListActivity",
                                "media": {
                                    "id": 123,
                                    "title": {"userPreferred": "Title"},
                                },
                            }
                        ],
                        "pageInfo": {"lastPage": 1},
                    }
                }
            },
        ),
        (
            200,
            {
                "data": {
                    "User": {"mediaListOptions": {"scoreFormat": "POINT_10"}},
                    "Page": {"mediaList": [{"mediaId": 123, "score": 9}]},
                }
            },
        ),
    ]

    async def request(*args: Any, **kwargs: Any) -> tuple[int, Any]:
        return responses.pop(0)

    cog._anilist_request = request  # type: ignore[method-assign]
    entries, page_count = await cog._fetch_recent_activity_page(42, None, 1)

    assert page_count == 1
    assert entries[0]["rating"] == 9
    assert entries[0]["_scoreFormat"] == "POINT_10"
    assert "\n-# 9/10" in _recent_activity_line(entries[0])


def test_recent_is_an_optional_standalone_hybrid_command() -> None:
    assert Anime.recent.name == "recent"
    assert Anime.recent.app_command is not None
    parameter = Anime.recent.app_command.get_parameter("user")
    assert parameter is not None and parameter.required is False
