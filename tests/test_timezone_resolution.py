# Test doubles supply only the Discord/service fields exercised by each test.
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from extensions.context import Context
from extensions.tools.reminders import (
    Reminder,
    TimeZone,
    TimeZoneDisambiguatorView,
    parse_utc_offset,
)
from utils.timezone_locations import OfflineLocationResolver


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("UTC+3", "UTC+03:00"),
        ("utc - 5", "UTC-05:00"),
        ("GMT+05:30", "UTC+05:30"),
        ("UTC-0330", "UTC-03:30"),
        ("UTC+14", "UTC+14:00"),
    ],
)
def test_parse_utc_offset(argument: str, expected: str) -> None:
    assert parse_utc_offset(argument) == TimeZone(expected, expected)


@pytest.mark.parametrize("argument", ["UTC+15", "UTC+14:01", "UTC+03:60", "UTC+"])
def test_parse_utc_offset_rejects_invalid_values(argument: str) -> None:
    assert parse_utc_offset(argument) is None


@pytest.fixture(scope="module")
def location_resolver() -> OfflineLocationResolver:
    return OfflineLocationResolver()


def test_location_resolver_supports_city_country_and_state(
    location_resolver: OfflineLocationResolver,
) -> None:
    assert {match.timezone for match in location_resolver.resolve("Paris")} >= {
        "Europe/Paris"
    }
    assert {match.timezone for match in location_resolver.resolve("Japan")} == {
        "Asia/Tokyo"
    }
    assert {match.timezone for match in location_resolver.resolve("Florida")} >= {
        "America/New_York",
        "America/Chicago",
    }


def test_location_resolver_keeps_ambiguous_place_names(
    location_resolver: OfflineLocationResolver,
) -> None:
    assert {match.timezone for match in location_resolver.resolve("Georgia")} >= {
        "America/New_York",
        "Asia/Tbilisi",
    }


def test_reminder_resolves_abbreviations_case_insensitively(
    location_resolver: OfflineLocationResolver,
) -> None:
    reminder = Reminder.__new__(Reminder)
    reminder.valid_timezones = {"UTC", "America/New_York"}
    reminder._timezone_aliases = {"EST": "America/New_York"}
    reminder._location_resolver = location_resolver

    assert reminder.resolve_timezones("est") == [TimeZone("EST", "America/New_York")]


def test_reminder_routes_unresolved_private_interactions_to_author_dm() -> None:
    interaction = SimpleNamespace(
        guild_id=None,
        channel=None,
        user=SimpleNamespace(id=1),
    )

    assert (
        Reminder._interaction_requires_author_dm(
            cast("discord.Interaction[discord.Client]", interaction)
        )
        is True
    )


def test_reminder_keeps_guild_interactions_in_the_source_channel() -> None:
    interaction = SimpleNamespace(
        guild_id=123,
        channel=None,
        user=SimpleNamespace(id=1),
    )

    assert (
        Reminder._interaction_requires_author_dm(
            cast("discord.Interaction[discord.Client]", interaction)
        )
        is False
    )


@pytest.mark.asyncio
async def test_timezone_buttons_paginate_all_matches() -> None:
    ctx = SimpleNamespace(author=SimpleNamespace(id=1))
    timezones = [
        TimeZone(f"Timezone {index}", f"Etc/GMT{index}") for index in range(21)
    ]
    view = TimeZoneDisambiguatorView(cast(Context, ctx), timezones)

    assert view.page_count == 2
    assert len(view.children) == 22
    assert "page 1/2" in view.content
