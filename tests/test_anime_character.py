from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord

from extensions.anime import (
    ANILIST_CHARACTER_QUERY,
    ANILIST_MEDIA_QUERY,
    ANILIST_PROFILE_QUERY,
    ANILIST_RATING_REMOVE_EMOJI,
    ANILIST_SAVE_MEDIA_MUTATION,
    ANILIST_SAVE_RATING_MUTATION,
    ANILIST_SMILEY_EMOJIS,
    ANILIST_TOGGLE_FAVOURITE_MUTATION,
    ANILIST_VIEWER_OPTIONS_QUERY,
    Anime,
    CharacterLookupView,
    MediaLookupView,
    MediaRatingChoiceView,
    MediaRatingModal,
    _character_age_value,
    _character_description_data,
    _character_description_parts,
    _character_media_text,
    _clean_about,
    _clean_character_description,
    _context_allows_adult_art,
    _description_preview,
    _favourite_media_entries,
    _media_description_parts,
    _media_enum_label,
    _media_next_airing_text,
    _media_status_description,
    _media_status_label,
    _media_user_rating,
    _normalise_media_rating,
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
    assert "alternativeSpoiler" in ANILIST_CHARACTER_QUERY
    assert "isAdult" in ANILIST_CHARACTER_QUERY
    assert "isAdult" in ANILIST_MEDIA_QUERY
    assert "isAdult" in ANILIST_PROFILE_QUERY
    assert Anime.character.app_command is not None
    parameter = Anime.character.app_command.get_parameter("search")
    assert parameter is not None
    assert "character name" in parameter.description


def test_adult_artwork_is_allowed_only_in_dms_or_nsfw_channels() -> None:
    assert _context_allows_adult_art(cast(Context, SimpleNamespace(guild=None))) is True
    assert (
        _context_allows_adult_art(
            cast(
                Context,
                SimpleNamespace(
                    guild=object(),
                    channel=SimpleNamespace(is_nsfw=lambda: False),
                ),
            )
        )
        is False
    )
    assert (
        _context_allows_adult_art(
            cast(
                Context,
                SimpleNamespace(
                    guild=object(),
                    channel=SimpleNamespace(is_nsfw=lambda: True),
                ),
            )
        )
        is True
    )


def test_favourite_entries_keep_adult_artwork_metadata() -> None:
    entries = _favourite_media_entries(
        {
            "favourites": {
                "anime": {
                    "nodes": [
                        {
                            "title": {"romaji": "Adult title"},
                            "siteUrl": "https://anilist.co/anime/1",
                            "isAdult": True,
                            "coverImage": {
                                "extraLarge": "https://example.com/adult.png"
                            },
                        }
                    ]
                }
            }
        },
        "anime",
    )
    assert entries[0][3] is True


def test_character_description_is_sanitized_for_discord() -> None:
    value = _clean_character_description("<b>Hello</b><br>@everyone **unsafe**")
    assert value.startswith("Hello\n")
    assert "@\u200beveryone" in value
    assert "\\*\\*unsafe\\*\\*" in value


def test_anilist_spoilers_and_links_survive_safe_markdown_cleanup() -> None:
    value = _clean_character_description(
        "[Emilia](https://anilist.co/character/88572) knows ~!a secret!~."
    )
    assert "[Emilia](https://anilist.co/character/88572)" in value
    assert "||a secret||" in value
    assert _clean_about("Story: ~!the ending!~") == "Story: ||the ending||"


def test_media_description_source_and_bullets_are_cleaned() -> None:
    value = (
        "Description.\n\n(Source: Dark Horse)\n\nNotes:\n"
        + chr(92)
        + "\n- Volumes 1-5 contain the prequel chapters."
    )

    description, source = _media_description_parts(value)

    assert source is None
    assert "Source:" not in description
    assert "\\-" not in description
    assert "Volumes 1-5" not in description


def test_media_description_notes_are_removed_without_trailing_content() -> None:
    description, source = _media_description_parts(
        "A short description.\n\nNotes:\n\n- First note.\n- Second note."
    )

    assert description == "A short description."
    assert source is None


def test_media_description_special_episode_appendix_is_removed() -> None:
    description, source = _media_description_parts(
        "A One Piece description.\n\n"
        "*This includes the following special episodes:\n\n"
        "- Chopperman to the Rescue! Protect the TV Station by the Shore! "
        "(Episode 336)\n\n"
        "- The Strongest Tag-Team! Luffy and Toriko's Hard Struggle! "
        "(Episode 492)"
    )

    assert description == "A One Piece description."
    assert source is None


def test_media_description_source_variants_are_removed() -> None:
    description, source = _media_description_parts(
        "A description.\n\n**Description source:** Dark Horse"
    )

    assert description == "A description."
    assert source is None


def test_media_description_keeps_complete_text_until_discord_limit() -> None:
    description, _ = _media_description_parts(
        "Enter Monkey D. Luffy, a 17-year-old boy that defies your standard "
        "definition of a pirate. Rather than the popular persona of a wicked, "
        "hardened, toothless pirate who ransacks villages for fun, Luffy's reason "
        "for being a pirate is one of pure wonder; the thought of an exciting "
        "adventure and meeting new and intriguing people, along with finding One "
        "Piece, are his reasons of becoming a pirate. Following in the footsteps "
        "of his childhood hero, Luffy and his crew travel across the Grand Line, "
        "experiencing crazy adventures, unveiling dark mysteries and battling "
        "strong enemies, all in order to reach One Piece."
    )

    assert "intriguing people" in description
    assert description.endswith("reach One Piece.")
    assert not description.endswith("...")


def test_description_preview_does_not_cut_an_anilist_link() -> None:
    value = (
        "A long description before the link. "
        "[Alvida](https://anilist.co/character/88572) "
        "The description continues after the link."
    )

    preview, truncated = _description_preview(value, max_length=65)

    assert truncated is True
    assert preview == "A long description before the link."


def test_description_preview_stops_at_a_sentence_boundary() -> None:
    preview, truncated = _description_preview(
        "First sentence. Second sentence. Third sentence.", max_length=25
    )

    assert truncated is True
    assert preview.startswith("First sentence.")
    assert "Second" not in preview


def test_character_age_drops_anilist_trailing_dash() -> None:
    assert _character_age_value("15-") == "15"
    assert _character_age_value("15-18") == "15-18"


def test_media_enum_labels_use_title_case() -> None:
    assert _media_enum_label("RELEASING") == "Releasing"
    assert _media_enum_label("ONE_SHOT") == "One Shot"


def test_media_planning_status_is_media_specific() -> None:
    assert _media_status_label("PLANNING", "anime") == "Plan to watch"
    assert _media_status_label("PLANNING", "manga") == "Plan to read"
    assert _media_status_description("PLANNING", "manga") == "Plan to read this manga"


def test_media_user_rating_uses_the_selected_score_format() -> None:
    media = {
        "mediaListEntry": {"score": 8.5},
        "_viewerScoreFormat": "POINT_10_DECIMAL",
    }
    assert _media_user_rating(media) == "8.5/10.0"
    media["mediaListEntry"]["score"] = 0.5
    assert _media_user_rating(media) == "0.5/10.0"
    media["_viewerScoreFormat"] = "POINT_100"
    media["mediaListEntry"]["score"] = 87
    assert _media_user_rating(media) == "87/100"
    media["_viewerScoreFormat"] = "POINT_5"
    media["mediaListEntry"]["score"] = 4
    assert _media_user_rating(media) == "4/5"
    media["_viewerScoreFormat"] = "POINT_3"
    media["mediaListEntry"]["score"] = 1
    assert _media_user_rating(media) == "😿"
    media["mediaListEntry"]["score"] = 2
    assert _media_user_rating(media) == "🐱"
    media["mediaListEntry"]["score"] = 3
    assert _media_user_rating(media) == "😸"
    media["_viewerScoreFormat"] = "SMILEY"
    assert _media_user_rating(media) == "😸"
    media["mediaListEntry"]["score"] = 0
    assert _media_user_rating(media) is None


def test_media_rating_input_supports_decimal_scores_and_limits() -> None:
    decimal_media = {"_viewerScoreFormat": "POINT_10_DECIMAL"}
    assert _normalise_media_rating(decimal_media, 0.5) == 0.5
    assert _normalise_media_rating(decimal_media, 8.56) == 8.6
    assert _normalise_media_rating(decimal_media, 12) == 10
    assert _normalise_media_rating(decimal_media, 0) == 0

    five_point_media = {"_viewerScoreFormat": "POINT_5"}
    assert _normalise_media_rating(five_point_media, 4.4) == 4
    assert _normalise_media_rating(five_point_media, 8) == 5


def test_media_query_requests_authenticated_list_details() -> None:
    assert "isFavourite" in ANILIST_MEDIA_QUERY
    assert "mediaListEntry { id status progress score }" in ANILIST_MEDIA_QUERY
    assert "nextAiringEpisode { airingAt episode }" in ANILIST_MEDIA_QUERY
    assert "mediaListOptions { scoreFormat }" in ANILIST_VIEWER_OPTIONS_QUERY
    assert "score: $score" not in ANILIST_SAVE_MEDIA_MUTATION
    assert "score: $score" in ANILIST_SAVE_RATING_MUTATION


def test_media_view_omits_description_source_from_details() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(bot=SimpleNamespace(embedcolor=discord.Colour.blurple())),
    )
    view = MediaLookupView(
        cast(Any, SimpleNamespace()),
        ctx,
        {
            "id": 30002,
            "title": {"userPreferred": "Berserk"},
            "description": "A description.\n\n(Source: Dark Horse)",
            "format": "MANGA",
        },
        None,
        "manga",
    )

    container = cast(discord.ui.Container, view.children[0])
    details = container.children[3]
    assert isinstance(details, discord.ui.TextDisplay)
    assert "Description source" not in details.content


def test_media_editor_controls_are_toggled_outside_the_main_container() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    view = MediaLookupView(
        cast(Any, SimpleNamespace()),
        ctx,
        {
            "id": 1,
            "title": {"userPreferred": "Example"},
            "mediaListEntry": {"status": "CURRENT", "progress": 1},
        },
        "token",
        "anime",
    )

    container = cast(discord.ui.Container, view.children[0])
    assert not any(isinstance(item, discord.ui.Select) for item in container.children)
    navigation = cast(discord.ui.ActionRow, view.children[-1])
    editor = next(
        item
        for item in navigation.children
        if isinstance(item, discord.ui.Button) and item.label == "Editor"
    )

    view.editor_open = True
    view._render()
    container = cast(discord.ui.Container, view.children[0])
    assert any(
        isinstance(child, discord.ui.ActionRow)
        and any(isinstance(item, discord.ui.Select) for item in child.children)
        for child in container.children
    )
    assert editor.label == "Editor"


def test_media_view_displays_release_and_authenticated_rating_details() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    view = MediaLookupView(
        cast(Any, SimpleNamespace()),
        ctx,
        {
            "id": 30002,
            "title": {"userPreferred": "Berserk"},
            "description": "A description.",
            "format": "MANGA",
            "status": "RELEASING",
            "isFavourite": True,
            "mediaListEntry": {"status": "CURRENT", "progress": 2, "score": 8},
            "_viewerScoreFormat": "POINT_10",
        },
        "token",
        "manga",
    )
    view.editor_open = True
    view._render()
    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "**Format:** Manga" in text
    assert "**Release status:** Releasing" in text
    assert "**Your rating:**" not in text
    assert "**Favourited:**" not in text
    favourite_button = next(
        button for button in _view_buttons(view) if str(button.emoji) == "❤️"
    )
    assert favourite_button.label is None
    rating_button = next(
        button for button in _view_buttons(view) if button.label == "8/10"
    )
    assert rating_button.style == discord.ButtonStyle.secondary
    list_status_index = next(
        index
        for index, child in enumerate(container.children)
        if isinstance(child, discord.ui.TextDisplay)
        and "**List status:**" in child.content
    )
    assert isinstance(container.children[list_status_index - 1], discord.ui.Separator)


def test_manga_planning_status_uses_plan_to_read_label() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    view = MediaLookupView(
        cast(Any, SimpleNamespace()),
        ctx,
        {
            "id": 30002,
            "title": {"userPreferred": "Berserk"},
            "mediaListEntry": {"status": "PLANNING", "progress": 0},
        },
        "token",
        "manga",
    )
    view.editor_open = True
    view._render()
    container = cast(discord.ui.Container, view.children[0])
    text = "\n".join(
        child.content
        for child in container.children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert "**List status:** Plan to read" in text
    select = next(
        item
        for child in container.children
        if isinstance(child, discord.ui.ActionRow)
        for item in child.children
        if isinstance(item, discord.ui.Select)
    )
    planning = next(option for option in select.options if option.value == "PLANNING")
    assert planning.label == "Plan to read"
    assert planning.default is True


def test_media_view_displays_next_episode_as_discord_timestamps() -> None:
    airing_at = 1_800_000_000
    assert (
        _media_next_airing_text(
            {"nextAiringEpisode": {"airingAt": airing_at, "episode": 7}}
        )
        == f"**Next episode:** Episode 7: <t:{airing_at}:R>"
    )

    ctx = cast(
        Context,
        SimpleNamespace(bot=SimpleNamespace(embedcolor=discord.Colour.blurple())),
    )
    view = MediaLookupView(
        cast(Any, SimpleNamespace()),
        ctx,
        {
            "id": 1,
            "title": {"userPreferred": "Example Anime"},
            "nextAiringEpisode": {"airingAt": airing_at, "episode": 7},
        },
        None,
        "anime",
    )
    text = "\n".join(
        child.content
        for child in cast(discord.ui.Container, view.children[0]).children
        if isinstance(child, discord.ui.TextDisplay)
    )
    assert f"<t:{airing_at}:R>" in text


def test_releasing_anime_shows_aired_and_next_episode() -> None:
    airing_at = 1_800_000_000
    assert (
        _media_next_airing_text(
            {
                "status": "RELEASING",
                "episodes": 19,
                "nextAiringEpisode": {"airingAt": airing_at, "episode": 12},
            }
        )
        == f"**Episodes:** 11/19 (Episode 12 airs <t:{airing_at}:R>)"
    )


async def test_favourite_toggle_uses_anilist_mutation_and_updates_button() -> None:
    class Cog:
        async def _anilist_request(
            self, query: str, variables: dict[str, Any], token: str | None
        ) -> tuple[int, Any]:
            assert query == ANILIST_TOGGLE_FAVOURITE_MUTATION
            assert variables == {"animeId": 1}
            assert token == "token"
            return 200, {"data": {"ToggleFavourite": {"anime": {"nodes": []}}}}

    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    media = {
        "id": 1,
        "title": {"userPreferred": "Example"},
        "mediaListEntry": {"status": "CURRENT", "progress": 1},
        "isFavourite": True,
    }
    view = MediaLookupView(cast(Any, Cog()), ctx, media, "token", "anime")
    view.editor_open = True
    view._render()
    favourite_button = next(
        button for button in _view_buttons(view) if str(button.emoji) == "❤️"
    )

    class Response:
        async def defer(self) -> None:
            return None

    class Followup:
        async def send(self, *args: Any, **kwargs: Any) -> None:
            return None

    class Interaction:
        response = Response()
        followup = Followup()

        async def edit_original_response(self, **kwargs: Any) -> None:
            return None

    await favourite_button.callback(cast(discord.Interaction, Interaction()))
    assert media["isFavourite"] is False
    assert any(str(button.emoji) == "🩶" for button in _view_buttons(view))


async def test_rating_button_opens_numeric_modal_with_remove_instructions() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    view = MediaLookupView(
        cast(Any, SimpleNamespace()),
        ctx,
        {
            "id": 1,
            "title": {"userPreferred": "Example"},
            "mediaListEntry": {"score": 4},
            "_viewerScoreFormat": "POINT_10",
        },
        "token",
        "anime",
    )
    view.editor_open = True
    view._render()
    rating_button = next(
        button for button in _view_buttons(view) if button.label == "4/10"
    )

    class Response:
        modal: Any = None

        async def send_modal(self, modal: Any) -> None:
            self.modal = modal

    class Interaction:
        response = Response()
        message = None

    await rating_button.callback(cast(discord.Interaction, Interaction()))
    assert isinstance(Interaction.response.modal, MediaRatingModal)
    assert "0 removes it" in str(Interaction.response.modal.rating.label)
    assert "0 removes your rating" in str(Interaction.response.modal.rating.placeholder)


async def test_smiley_rating_view_updates_score_and_has_remove_button() -> None:
    class Cog:
        def __init__(self) -> None:
            self.bot = SimpleNamespace(logger=SimpleNamespace(debug=lambda *args: None))
            self.scores: list[float] = []

        async def _anilist_request(
            self, query: str, variables: dict[str, Any], token: str | None
        ) -> tuple[int, Any]:
            assert "score: $score" in query
            assert variables["mediaId"] == 1
            self.scores.append(variables["score"])
            assert token == "token"
            return 200, {
                "data": {
                    "SaveMediaListEntry": {
                        "id": 1,
                        "score": variables["score"],
                    }
                }
            }

    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    media = {
        "id": 1,
        "title": {"userPreferred": "Example"},
        "mediaListEntry": {"score": 2},
        "_viewerScoreFormat": "POINT_3",
    }
    view = MediaLookupView(cast(Any, Cog()), ctx, media, "token", "anime")
    view.editor_open = True
    view._render()
    rating_button = next(
        button
        for button in _view_buttons(view)
        if button.label == ANILIST_SMILEY_EMOJIS[1]
    )

    class SourceMessage:
        async def edit(self, **kwargs: Any) -> None:
            return None

    class Response:
        sent_view: Any = None

        async def send_message(self, **kwargs: Any) -> None:
            self.sent_view = kwargs["view"]

    class OpenInteraction:
        response = Response()
        message = SourceMessage()

    await rating_button.callback(cast(discord.Interaction, OpenInteraction()))
    choice_view = OpenInteraction.response.sent_view
    assert isinstance(choice_view, MediaRatingChoiceView)
    smile_button = next(
        button
        for button in _view_buttons(choice_view)
        if str(button.emoji) == ANILIST_SMILEY_EMOJIS[2]
    )
    remove_button = next(
        button
        for button in _view_buttons(choice_view)
        if str(button.emoji) == ANILIST_RATING_REMOVE_EMOJI
    )
    assert any(
        str(button.emoji) == ANILIST_RATING_REMOVE_EMOJI
        for button in _view_buttons(choice_view)
    )
    assert any(
        str(button.emoji) == ANILIST_SMILEY_EMOJIS[2]
        for button in _view_buttons(choice_view)
    )

    class ChoiceResponse:
        async def defer(self) -> None:
            return None

    class ChoiceInteraction:
        response = ChoiceResponse()
        message = SourceMessage()

        async def edit_original_response(self, **kwargs: Any) -> None:
            return None

    # The smile button stores the highest score, while the trash button clears it.
    await smile_button.callback(cast(discord.Interaction, ChoiceInteraction()))
    assert media["mediaListEntry"]["score"] == 3
    await remove_button.callback(cast(discord.Interaction, ChoiceInteraction()))
    assert media["mediaListEntry"]["score"] == 0


def test_media_view_hides_adult_cover_in_regular_channels() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            guild=object(),
            channel=SimpleNamespace(is_nsfw=lambda: False),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    view = MediaLookupView(
        cast(Any, SimpleNamespace()),
        ctx,
        {
            "id": 1,
            "title": {"userPreferred": "Adult title"},
            "description": "Description",
            "isAdult": True,
            "coverImage": {"extraLarge": "https://example.com/adult.png"},
        },
        None,
        "anime",
    )
    container = cast(discord.ui.Container, view.children[0])
    assert not any(
        isinstance(child, discord.ui.Section) for child in container.children
    )


def test_character_view_hides_adult_art_in_regular_channels() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            guild=object(),
            channel=SimpleNamespace(is_nsfw=lambda: False),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    view = CharacterLookupView(
        ctx,
        {
            "id": 1,
            "name": {"full": "Adult character"},
            "image": {"large": "https://example.com/adult.png"},
            "media": {"nodes": [{"isAdult": True}]},
        },
    )
    container = cast(discord.ui.Container, view.children[0])
    assert not any(
        isinstance(child, discord.ui.Section) for child in container.children
    )


def _view_buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button]:
    container = cast(discord.ui.Container, view.children[0])
    buttons: list[discord.ui.Button] = []
    for item in container.children:
        if isinstance(item, discord.ui.ActionRow):
            buttons.extend(
                child for child in item.children if isinstance(child, discord.ui.Button)
            )
    return buttons


async def test_character_description_and_appearance_buttons() -> None:
    ctx = cast(
        Context,
        SimpleNamespace(
            author=SimpleNamespace(id=1),
            bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
        ),
    )
    view = CharacterLookupView(
        ctx,
        {
            "id": 40882,
            "name": {"full": "Eren Yeager"},
            "description": " ".join(["A complete sentence."] * 60),
            "media": {
                "nodes": [
                    {
                        "type": "ANIME",
                        "siteUrl": "https://anilist.co/anime/1",
                        "title": {"userPreferred": "Example"},
                    }
                ]
            },
        },
    )
    buttons = _view_buttons(view)
    assert [button.label for button in buttons] == ["...", "Show appearances"]

    class Response:
        async def edit_message(self, **kwargs: Any) -> None:
            return None

    interaction = cast(
        discord.Interaction,
        SimpleNamespace(user=SimpleNamespace(id=1), response=Response()),
    )
    await buttons[0].callback(interaction)
    assert "^" in [button.label for button in _view_buttons(view)]
    await next(
        button for button in _view_buttons(view) if button.label == "^"
    ).callback(interaction)
    assert "..." in [button.label for button in _view_buttons(view)]

    show_button = next(
        button for button in _view_buttons(view) if button.label == "Show appearances"
    )
    await show_button.callback(interaction)
    assert all(button.label != "Show appearances" for button in _view_buttons(view))
    container = cast(discord.ui.Container, view.children[0])
    assert any(
        isinstance(item, discord.ui.TextDisplay)
        and item.content.startswith("### Appears in")
        for item in container.children
    )


def test_character_height_is_moved_out_of_the_description() -> None:
    description, height = _character_description_parts(
        "A character description.<br><br>**Height:** 164 cm"
    )
    assert description == "A character description."
    assert height == "164 cm"


def test_character_metadata_is_moved_out_of_broken_description_markdown() -> None:
    description, metadata = _character_description_data(
        "__Height:__ 174 cm\n"
        "__Affiliation:__ Straw Hat Pirates\n"
        "__Bounty:__ ~!500,000,000!~\n\n"
        "A character description."
    )

    assert description == "A character description."
    assert metadata["height"] == "174 cm"
    assert metadata["affiliation"] == "Straw Hat Pirates"
    assert metadata["bounty"] == "||500,000,000||"


def test_spoiler_prefixed_character_metadata_is_moved_to_details() -> None:
    description, metadata = _character_description_data(
        "~!**True Devil Fruit:** Hito Hito no Mi Model: Nika "
        "(Human-Human Fruit)!~\n\nA character description."
    )

    assert description == "A character description."
    assert "True Devil Fruit" not in description
    assert metadata["true devil fruit"] == (
        "||Hito Hito no Mi Model: Nika (Human-Human Fruit)||"
    )


def test_trailing_spoiler_marker_is_preserved_for_character_metadata() -> None:
    description, metadata = _character_description_data(
        "**True Devil Fruit Type:** Mythical Zoan!~\n\nA character description."
    )

    assert description == "A character description."
    assert metadata["true devil fruit type"] == "||Mythical Zoan||"


def test_character_appearances_are_spoilered() -> None:
    value = _character_media_text(
        {
            "media": {
                "nodes": [
                    {
                        "type": "ANIME",
                        "siteUrl": "https://anilist.co/anime/1",
                        "title": {"userPreferred": "Spoiler Anime"},
                    }
                ]
            }
        }
    )
    assert value == "[Spoiler Anime](https://anilist.co/anime/1) (Anime)"


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
            "alternativeSpoiler": ["Spoiler Alias"],
        },
        "image": {"large": "https://example.com/character.png"},
        "description": "A description.<br>**Height:** 164 cm",
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
    container = view.children[0]
    details = container.children[2]
    assert isinstance(details, discord.ui.TextDisplay)
    assert "**Other names:** Alias, ||Spoiler Alias||" in details.content
