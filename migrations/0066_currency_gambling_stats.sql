-- Durable aggregate totals for currency gambling outcomes.

CREATE TABLE IF NOT EXISTS currency_gambling_stats (
    user_id BIGINT PRIMARY KEY REFERENCES currency_wallets(user_id)
        ON DELETE CASCADE,
    total_wagered BIGINT NOT NULL DEFAULT 0 CHECK (total_wagered >= 0),
    total_earned BIGINT NOT NULL DEFAULT 0 CHECK (total_earned >= 0),
    total_lost BIGINT NOT NULL DEFAULT 0 CHECK (total_lost >= 0),
    wins BIGINT NOT NULL DEFAULT 0 CHECK (wins >= 0),
    losses BIGINT NOT NULL DEFAULT 0 CHECK (losses >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS currency_gambling_stats_wins_idx
    ON currency_gambling_stats (wins DESC, user_id ASC);

