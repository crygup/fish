-- Keep the payout reached by a streak game alongside its personal bests.
-- Payouts are only recorded once a wager is successfully cashed out.
ALTER TABLE streak_game_stats
    ADD COLUMN IF NOT EXISTS highest_streak_payout BIGINT NOT NULL DEFAULT 0
        CHECK (highest_streak_payout >= 0),
    ADD COLUMN IF NOT EXISTS highest_payout BIGINT NOT NULL DEFAULT 0
        CHECK (highest_payout >= 0),
    ADD COLUMN IF NOT EXISTS highest_payout_streak BIGINT NOT NULL DEFAULT 0
        CHECK (highest_payout_streak >= 0);

CREATE INDEX IF NOT EXISTS streak_game_stats_payout_leaderboard_idx
    ON streak_game_stats (game, highest_payout DESC, highest_payout_streak DESC, user_id);
