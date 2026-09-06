from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

from extensions.fun.birthday import Birthday, birthday_timestamp, parse_birthday


@pytest.mark.parametrize(
    "text",
    [
        "8.18.98",
        "8/18/1998",
        "8-18-98",
        "August 18th 1998",
        "18 aug 1998",
        "1998-08-18",
        "18/8/98",
    ],
)
def test_birthday_formats(text):
    assert parse_birthday(text, today=date(2026, 9, 5)) == Birthday(8, 18, 1998)


@pytest.mark.parametrize("text", ["8.18", "8/18", "8-18", "August 18th", "aug 18th"])
def test_optional_year(text):
    assert parse_birthday(text) == Birthday(8, 18)


def test_year_and_ambiguity_rules():
    today = date(2026, 9, 5)
    assert parse_birthday("aug 18 26", today=today).year == 2026
    assert parse_birthday("aug 18 27", today=today).year == 1927
    assert parse_birthday("2002 sep 4th", today=today) == Birthday(9, 4, 2002)
    assert parse_birthday("4/5", today=today) == Birthday(4, 5)


@pytest.mark.parametrize(
    "text", ["February 30", "2/29/2001", "August", "1998", "0/18", "8/18/2098"]
)
def test_invalid_dates(text):
    with pytest.raises(ValueError):
        parse_birthday(text, today=date(2026, 9, 5))


def test_labels_and_next_occurrence():
    assert Birthday(8, 18, 1998).label() == "August 18th, 1998"
    assert Birthday(1, 21).label() == "January 21st"
    assert Birthday(2, 29).next_date(date(2026, 9, 5)) == date(2028, 2, 29)
    assert Birthday(8, 18).next_date(date(2026, 9, 5)) == date(2027, 8, 18)


@pytest.mark.parametrize(
    "viewer_zone,owner_zone,expected_hour",
    [
        ("America/New_York", "Asia/Tokyo", 4),
        (None, "America/New_York", 4),
        (None, None, 0),
        ("invalid/zone", "America/New_York", 4),
    ],
)
async def test_birthday_timezone_priority(viewer_zone, owner_zone, expected_hour):
    pool = AsyncMock()
    pool.fetch.return_value = [
        {"user_id": 1, "timezone": viewer_zone},
        {"user_id": 2, "timezone": owner_zone},
    ]
    stamp = await birthday_timestamp(
        pool, Birthday(8, 18), 1, 2,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert (stamp.month, stamp.day, stamp.hour) == (8, 18, 0)
    assert stamp.astimezone(timezone.utc).hour == expected_hour


async def test_birthday_uses_local_date_at_year_boundary():
    pool = AsyncMock()
    pool.fetch.return_value = [{"user_id": 1, "timezone": "America/New_York"}]
    stamp = await birthday_timestamp(
        pool, Birthday(12, 31), 1, 1,
        now=datetime(2027, 1, 1, 1, tzinfo=timezone.utc),
    )
    assert stamp.year == 2026
    assert stamp.hour == 0
