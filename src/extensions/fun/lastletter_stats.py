"""Persistence helpers for LastLetter results."""

from __future__ import annotations

from typing import Any

from core.currency import award_daily_capped_coins

LASTLETTER_DAILY_REWARD_CAP = 5_000


async def record_lastletter_result(
    pool: Any,
    user_id: int,
    *,
    won: bool,
    tracking_enabled: bool = True,
) -> None:
    """Record one LastLetter result when game-history tracking is enabled."""

    if not tracking_enabled:
        return
    wins = 1 if won else 0
    losses = 0 if won else 1
    await pool.execute(
        """
        INSERT INTO lastletter_stats (user_id, wins, losses)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET
            wins = lastletter_stats.wins + EXCLUDED.wins,
            losses = lastletter_stats.losses + EXCLUDED.losses,
            updated_at = now()
        """,
        int(user_id),
        wins,
        losses,
    )


async def award_lastletter_winner_coins(pool: Any, user_id: int, amount: int) -> int:
    """Award LastLetter winnings subject to the game's daily reward cap.

    LastLetter has no wager, so its reward must be bounded independently of
    the number of lobbies a user starts.  The currency service performs the
    check and credit atomically, including across multiple bot instances.
    """

    return await award_daily_capped_coins(
        pool,
        int(user_id),
        int(amount),
        LASTLETTER_DAILY_REWARD_CAP,
        "lastletter",
    )
