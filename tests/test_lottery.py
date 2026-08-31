from __future__ import annotations

from datetime import datetime, timezone

from core.currency import LotteryStatus, lottery_period_start, lottery_starting_pool


def test_lottery_period_start_uses_utc_hour() -> None:
    value = lottery_period_start(
        datetime(2026, 8, 25, 23, 59, 41, 123456, tzinfo=timezone.utc)
    )
    assert value == datetime(2026, 8, 25, 23, tzinfo=timezone.utc)


def test_lottery_status_chance_is_ticket_based() -> None:
    status = LotteryStatus(
        round_start=datetime(2026, 8, 25, 23, tzinfo=timezone.utc),
        prize_pool=1_500,
        total_tickets=8,
        user_tickets=2,
    )
    assert status.chance_percent == 25.0


def test_lottery_status_with_no_tickets_has_no_chance() -> None:
    status = LotteryStatus(
        round_start=datetime(2026, 8, 25, 23, tzinfo=timezone.utc),
        prize_pool=1_000,
    )
    assert status.chance_percent == 0.0


def test_lottery_starting_pool_scales_without_a_bonus_cap() -> None:
    assert lottery_starting_pool() == 1_000
    assert lottery_starting_pool(25) == 1_075
    assert lottery_starting_pool(10_000) == 31_000
