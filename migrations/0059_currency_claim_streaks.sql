-- Store recurring daily and weekly Coin claim streaks.
ALTER TABLE currency_claims
    ADD COLUMN IF NOT EXISTS streak INTEGER NOT NULL DEFAULT 0
        CHECK (streak >= 0),
    ADD COLUMN IF NOT EXISTS streak_bonus_amount BIGINT NOT NULL DEFAULT 0
        CHECK (streak_bonus_amount >= 0);
