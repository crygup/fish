from __future__ import annotations

import datetime

from test_support import not_none

from utils.anilist import (
    anilist_airing_datetime,
    anilist_datetime,
    anilist_search_variants,
    latest_anilist_successor,
    normalize_anilist_title,
    select_anilist_media,
)


def test_anilist_title_normalization_handles_space_and_apostrophe_variants() -> None:
    assert normalize_anilist_title("  The Guy  She Was Interested In  ") == (
        "the guy she was interested in"
    )
    assert normalize_anilist_title("Wasn’t a Guy At All") == "wasnt a guy at all"


def test_anilist_search_variants_keep_full_title_and_add_punctuation_fallback() -> None:
    value = "The Guy She Was Interested In Wasn't a Guy At All"
    assert anilist_search_variants(value) == (
        value,
        "The Guy She Was Interested In Wasn t a Guy At All",
    )


def test_select_anilist_media_prefers_exact_title_in_search_page() -> None:
    results = [
        {"id": 1, "title": {"userPreferred": "A Similar Title"}},
        {
            "id": 2,
            "title": {
                "userPreferred": "The Guy She Was Interested In Wasn't a Guy At All"
            },
        },
    ]
    assert (
        not_none(select_anilist_media(results, results[1]["title"]["userPreferred"]))[
            "id"
        ]
        == 2
    )


def test_anilist_dates_default_missing_components_to_january_first() -> None:
    assert anilist_datetime(
        {"year": 2026, "month": 8, "day": None}
    ) == datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    assert anilist_datetime(
        {"year": 2026, "month": None, "day": None}
    ) == datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    assert anilist_datetime({"year": None, "month": 8, "day": 2}) is None


def test_latest_anilist_successor_prefers_the_newest_scheduled_successor() -> None:
    media = {
        "id": 10,
        "relations": {
            "edges": [
                {
                    "relationType": "SEQUEL",
                    "node": {
                        "id": 11,
                        "title": {"userPreferred": "Season 2"},
                        "startDate": {"year": 2025, "month": 1, "day": 1},
                    },
                },
                {
                    "relationType": "SEQUEL",
                    "node": {
                        "id": 12,
                        "title": {"userPreferred": "Season 3"},
                        "nextAiringEpisode": {"airingAt": 1_900_000_000, "episode": 1},
                    },
                },
            ]
        },
    }
    successor = latest_anilist_successor(media)
    assert successor is not None
    assert successor["id"] == 12
    airing, episode = anilist_airing_datetime(successor["nextAiringEpisode"])
    assert airing is not None
    assert episode == 1
