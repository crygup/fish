from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.anime import (
    ANILIST_CHARACTER_QUERY,
    Anime,
    CharacterLookupView,
    _clean_character_description,
)
from extensions.context import Context


def test_character_command_has_requested_aliases_and_search_description() -> None:
    assert set(Anime.character.aliases) == {
        "anime-character",
        "animecharacter",
        "acharacter",
        "achar",
    }
    assert "characters(search: $search" in ANILIST_CHARACTER_QUERY
    assert Anime.character.app_command is not None
    parameter = Anime.character.app_command.get_parameter("search")
    assert parameter is not None
    assert "character name" in parameter.description


def test_character_description_is_sanitized_for_discord() -> None:
    value = _clean_character_description("<b>Hello</b><br>@everyone **unsafe**")
    assert value.startswith("Hello\n")
    assert "@\u200beveryone" in value
    assert "\\*\\*unsafe\\*\\*" in value


def test_character_view_renders_profile_details_and_related_media() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(bot=SimpleNamespace(embedcolor=discord.Colour.blurple())),
    )
    character: dict[str, Any] = {
        "id": 1,
        "siteUrl": "https://anilist.co/character/1",
        "name": {
            "full": "Example Character",
            "native": "Example",
            "alternative": ["Alias"],
        },
        "image": {"large": "https://example.com/character.png"},
        "description": "A description.",
        "gender": "Female",
        "age": "18",
        "dateOfBirth": {"year": 2000, "month": 1, "day": 2},
        "favourites": 42,
        "media": {
            "nodes": [
                {
                    "id": 2,
                    "type": "ANIME",
                    "siteUrl": "https://anilist.co/anime/2",
                    "title": {"userPreferred": "Example Anime"},
                }
            ]
        },
    }

    view = CharacterLookupView(ctx, character)

    assert len(view.children) == 1
    assert isinstance(view.children[0], discord.ui.Container)
