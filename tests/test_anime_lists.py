from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.anime import (
    ANILIST_CURRENT_LIST_PUBLIC_QUERY,
    ANILIST_CURRENT_LIST_QUERY,
    Anime,
    MediaListView,
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
