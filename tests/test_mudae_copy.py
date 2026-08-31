from __future__ import annotations

from datetime import datetime, timezone

import pytest

from extensions.mudae.copy import (
    InvalidSourceGuild,
    InvalidWishCopyMode,
    SameWishCopyGuild,
    copy_types,
    parse_copy_mode,
    parse_source_guild_id,
    prepare_wish_copy,
    validate_copy_scope,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("wishlist", "wishlist"),
        ("characters", "wishlist"),
        ("series-wishlist", "serieswishlist"),
        ("series wishlist ka", "serieswishlistka"),
        ("serieswishlistkakera", "serieswishlistka"),
        ("everything", "all"),
    ],
)
def test_copy_mode_aliases_are_canonical(value: str, expected: str) -> None:
    assert parse_copy_mode(value) == expected


def test_copy_mode_rejects_unknown_values() -> None:
    with pytest.raises(InvalidWishCopyMode):
        parse_copy_mode("series-wishlist-kakera-maybe")

    assert copy_types("wishlist") == frozenset({"character"})
    assert copy_types("serieswishlist") == frozenset({"series"})
    assert copy_types("serieswishlistka") == frozenset({"series_kakera"})
    assert copy_types("all") == frozenset(
        {"character", "series", "series_kakera", "kakera"}
    )


@pytest.mark.parametrize("value", [None, "", "server-name", "<#123>", True, -1])
def test_source_guild_id_rejects_non_ids(value: object) -> None:
    with pytest.raises(InvalidSourceGuild):
        parse_source_guild_id(value)


def test_validate_copy_scope_rejects_same_or_inaccessible_source() -> None:
    with pytest.raises(SameWishCopyGuild):
        validate_copy_scope(123, 123)

    with pytest.raises(InvalidSourceGuild):
        validate_copy_scope(123, 456, available_guild_ids=[789])

    assert validate_copy_scope(123, 456, available_guild_ids=[123, 789]) == (123, 456)


def test_prepare_wish_copy_selects_and_normalises_rows() -> None:
    created = datetime(2026, 8, 27, tzinfo=timezone.utc)
    rows = [
        {
            "guild_id": 123,
            "user_id": 42,
            "wish_type": "character",
            "wish_value": "  Riza   Hawkeye ",
            "created_at": created,
            "id": 99,
        },
        # Series names are case/whitespace insensitive in notifications.
        {
            "guild_id": 123,
            "user_id": 42,
            "wish_type": "series",
            "wish_value": " JoJo's\nBizarre Adventure ",
        },
        # Legacy rows represented series-kakera as series + threshold.
        {
            "guild_id": 123,
            "user_id": 42,
            "wish_type": "series",
            "wish_value": "Demon Slayer",
            "kakera_threshold": "100",
        },
        {
            "guild_id": 123,
            "user_id": 42,
            "wish_type": "series_kakera",
            "wish_value": "Jujutsu Kaisen",
            "kakera_threshold": 200,
            "source_bundle_created_at": created,
        },
        {"guild_id": 123, "user_id": 42, "wish_type": "kakera", "wish_value": "1,000"},
        # Duplicate normalised character, unknown type, malformed value, and
        # another user's row are safely ignored.
        {
            "guild_id": 123,
            "user_id": 42,
            "wish_type": "character",
            "wish_value": "RIZA HAWKEYE",
        },
        {"guild_id": 123, "user_id": 42, "wish_type": "unknown", "wish_value": "x"},
        {"guild_id": 123, "user_id": 42, "wish_type": "character", "wish_value": "   "},
        {
            "guild_id": 123,
            "user_id": 999,
            "wish_type": "character",
            "wish_value": "other",
        },
    ]

    character = prepare_wish_copy(
        rows,
        mode="wishlist",
        source_guild_id=123,
        source_user_id=42,
    )
    assert [(row.wish_type, row.wish_value) for row in character] == [
        ("character", "riza hawkeye")
    ]
    assert character[0].source_guild_id == 123
    assert character[0].source_created_at == created

    series = prepare_wish_copy(
        rows,
        mode="serieswishlist",
        source_guild_id=123,
        source_user_id=42,
    )
    assert [(row.wish_type, row.wish_value) for row in series] == [
        ("series", "jojo's bizarre adventure")
    ]

    series_kakera = prepare_wish_copy(
        rows,
        mode="serieswishlistka",
        source_guild_id=123,
        source_user_id=42,
    )
    assert [
        (row.wish_type, row.wish_value, row.kakera_threshold) for row in series_kakera
    ] == [
        ("series_kakera", "demon slayer", 100),
        ("series_kakera", "jujutsu kaisen", 200),
    ]

    all_rows = prepare_wish_copy(
        rows,
        mode="all",
        source_guild_id=123,
        source_user_id=42,
    )
    assert {(row.wish_type, row.wish_value) for row in all_rows} == {
        ("character", "riza hawkeye"),
        ("series", "jojo's bizarre adventure"),
        ("series_kakera", "demon slayer"),
        ("series_kakera", "jujutsu kaisen"),
        ("kakera", "1000"),
    }


def test_copy_row_insert_values_replace_identity_and_keep_threshold() -> None:
    row = prepare_wish_copy(
        [
            {
                "guild_id": 123,
                "user_id": 42,
                "wish_type": "series_kakera",
                "wish_value": "Demon Slayer",
                "kakera_threshold": 100,
                "id": 55,
            }
        ],
        mode="all",
        source_guild_id=123,
        source_user_id=42,
    )[0]
    assert row.insert_values(destination_guild_id=456, user_id=42) == {
        "guild_id": 456,
        "user_id": 42,
        "wish_type": "series_kakera",
        "wish_value": "demon slayer",
        "kakera_threshold": 100,
    }
