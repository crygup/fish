"""Persistence helpers for Word Bomb results and rewards.

The command/view implementation owns its in-memory lobby and turn state.  This
module deliberately contains only small, atomic database operations so the
game can be reloaded or moved without duplicating persistence logic.
"""

from __future__ import annotations

from typing import Any

from core.currency import award_daily_capped_coins


async def record_wordbomb_result(
    pool: Any,
    user_id: int,
    *,
    won: bool,
    tracking_enabled: bool = True,
) -> None:
    """Record one Word Bomb win or loss.

    ``tracking_enabled`` should be supplied from ``bot.db_cache`` by the game
    cog.  As with the other game stats, disabling game tracking skips the row
    entirely while keeping the active game playable.
    """

    if not tracking_enabled:
        return
    wins = 1 if won else 0
    losses = 0 if won else 1
    await pool.execute(
        """
        INSERT INTO wordbomb_stats (user_id, wins, losses)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET
            wins = wordbomb_stats.wins + EXCLUDED.wins,
            losses = wordbomb_stats.losses + EXCLUDED.losses,
            updated_at = now()
        """,
        int(user_id),
        wins,
        losses,
    )


async def award_wordbomb_winner_coins(pool: Any, user_id: int) -> int:
    """Award the Word Bomb winner 100 Coins, capped at 5,000 per UTC day."""

    return await award_daily_capped_coins(
        pool,
        int(user_id),
        100,
        5_000,
        "wordbomb",
    )
