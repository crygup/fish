# Test doubles supply only the Discord/service fields exercised by each test.
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import discord
import pytest

from core.cache import db_cache
from extensions.tools.reputation import (
    REPUTATION_GUILD_BONUS_COINS,
    REPUTATION_USER_BONUS_COINS,
    TATSU_REPUTATION_PATTERN,
    Reputation,
    guild_period_start,
    next_guild_reset,
    next_user_reset,
    reputation_bonus_amount,
    user_period_start,
)


def test_reputation_periods_start_at_utc_boundaries() -> None:
    now = datetime(2026, 8, 17, 21, 22, tzinfo=timezone.utc)  # Monday
    assert user_period_start(now).isoformat() == "2026-08-17"
    assert guild_period_start(now).isoformat() == "2026-08-16"
    assert next_user_reset(now).isoformat() == "2026-08-18T00:00:00+00:00"
    assert next_guild_reset(now).isoformat() == "2026-08-23T00:00:00+00:00"


def test_tatsu_reputation_payload_extracts_text_recipient() -> None:
    match = TATSU_REPUTATION_PATTERN.search(
        "<:Reputation_Icon:745225325063176252> **crygup has given "
        "<@1116395117071306903> a reputation point!**"
    )
    assert match is not None
    assert match.group("giver") == "crygup"
    assert match.group("receiver") == "1116395117071306903"


def test_tatsu_reputation_payload_matches_slash_response() -> None:
    match = TATSU_REPUTATION_PATTERN.search(
        "<:Reputation_Icon:745225325063176252> **subwaycustomer. has given "
        "<@766953372309127168> a reputation point!**"
    )
    assert match is not None
    assert match.group("giver") == "subwaycustomer."
    assert match.group("receiver") == "766953372309127168"


def test_reputation_bonus_amounts_are_source_specific() -> None:
    assert REPUTATION_USER_BONUS_COINS == 500
    assert REPUTATION_GUILD_BONUS_COINS == 1_000
    assert (
        reputation_bonus_amount(
            kind="user",
            receiver_id=766953372309127168,
            guild_id=848507662437449750,
            source="tatsu",
        )
        == 500
    )
    assert (
        reputation_bonus_amount(
            kind="guild",
            receiver_id=None,
            guild_id=848507662437449750,
            source="fishie",
        )
        == 1_000
    )
    assert (
        reputation_bonus_amount(
            kind="guild",
            receiver_id=None,
            guild_id=848507662437449750,
            source="tatsu",
        )
        == 0
    )


def test_tatsu_interaction_user_id_accepts_discord_payload_shapes() -> None:
    object_message = SimpleNamespace(
        interaction_metadata=SimpleNamespace(
            user=SimpleNamespace(id=1127694734270419116)
        ),
        interaction=None,
    )
    mapping_message = SimpleNamespace(
        interaction_metadata={"user": {"id": "1127694734270419116"}},
        interaction=None,
    )
    assert (
        Reputation._interaction_user_id(cast("discord.Message", object_message))
        == 1127694734270419116
    )
    assert (
        Reputation._interaction_user_id(cast("discord.Message", mapping_message))
        == 1127694734270419116
    )


@pytest.mark.asyncio
async def test_tatsu_giver_resolution_queries_uncached_username() -> None:
    member = SimpleNamespace(name="giver_name")
    guild = SimpleNamespace(members=[], query_members=AsyncMock(return_value=[member]))
    reputation = Reputation.__new__(Reputation)

    resolved = await reputation._resolve_tatsu_giver(
        cast("discord.Guild", guild), "giver_name"
    )

    assert resolved is member
    guild.query_members.assert_awaited_once_with(
        query="giver_name", limit=100, cache=True
    )


def test_reputation_bonus_cache_stacks_sources_and_expires() -> None:
    cache = db_cache()
    now = datetime(2026, 8, 17, 21, 22, tzinfo=timezone.utc)

    cache.reset_reputation_bonus_cache(now)
    cache.add_reputation_user_bonus(42, "fishie", now)
    cache.add_reputation_user_bonus(42, "tatsu", now)
    cache.add_reputation_guild_bonus(42, "fishie", now)
    assert cache.reputation_bonus_count(42, now) == 3

    next_day = datetime(2026, 8, 18, 0, 1, tzinfo=timezone.utc)
    assert cache.reputation_bonus_count(42, next_day) == 1

    next_week = datetime(2026, 8, 23, 0, 1, tzinfo=timezone.utc)
    assert cache.reputation_bonus_count(42, next_week) == 0
