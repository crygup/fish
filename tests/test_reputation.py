from datetime import datetime, timezone

from core.cache import db_cache
from extensions.tools.reputation import (
    TATSU_REPUTATION_PATTERN,
    guild_period_start,
    next_guild_reset,
    next_user_reset,
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
