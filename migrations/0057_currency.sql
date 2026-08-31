-- Global Coins wallets, recurring claims, capped rewards, and durable wagers.

CREATE TABLE IF NOT EXISTS currency_wallets (
    user_id BIGINT PRIMARY KEY,
    balance BIGINT NOT NULL DEFAULT 0
        CHECK (balance >= 0 AND balance <= 9000000000000000000),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS currency_transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id)
        ON DELETE CASCADE,
    amount BIGINT NOT NULL CHECK (amount <> 0),
    source TEXT NOT NULL,
    reference_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS currency_transactions_reference_idx
    ON currency_transactions (user_id, reference_key)
    WHERE reference_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS currency_transactions_user_created_idx
    ON currency_transactions (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS currency_claims (
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id)
        ON DELETE CASCADE,
    claim_type TEXT NOT NULL CHECK (claim_type IN ('daily', 'weekly')),
    period_start DATE NOT NULL,
    base_amount BIGINT NOT NULL CHECK (base_amount > 0),
    bonus_amount BIGINT NOT NULL DEFAULT 0 CHECK (bonus_amount >= 0),
    claimed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, claim_type, period_start)
);

CREATE TABLE IF NOT EXISTS currency_daily_rewards (
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id)
        ON DELETE CASCADE,
    source TEXT NOT NULL,
    period_start DATE NOT NULL,
    amount BIGINT NOT NULL CHECK (amount >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, source, period_start)
);

CREATE TABLE IF NOT EXISTS currency_wagers (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id)
        ON DELETE CASCADE,
    source TEXT NOT NULL,
    stake BIGINT NOT NULL CHECK (stake BETWEEN 1 AND 100),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'lost', 'cashed_out')),
    payout BIGINT NOT NULL DEFAULT 0
        CHECK (payout >= 0 AND payout <= 1000000000000000000),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    settled_at TIMESTAMP WITH TIME ZONE,
    CHECK (
        (status = 'open' AND payout = 0 AND settled_at IS NULL)
        OR (status = 'lost' AND payout = 0 AND settled_at IS NOT NULL)
        OR (status = 'cashed_out' AND payout > 0 AND settled_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS currency_wagers_user_created_idx
    ON currency_wagers (user_id, created_at DESC);
