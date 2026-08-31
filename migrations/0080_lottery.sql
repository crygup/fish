-- Hourly Coins lottery rounds and active ticket inventory.

CREATE TABLE IF NOT EXISTS lottery_rounds (
    round_start TIMESTAMP WITH TIME ZONE PRIMARY KEY,
    prize_pool BIGINT NOT NULL DEFAULT 1000
        CHECK (prize_pool >= 1000 AND prize_pool <= 9000000000000000000),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'drawn', 'no_winner')),
    winning_ticket CHAR(6),
    winner_user_id BIGINT REFERENCES currency_wallets(user_id)
        ON DELETE SET NULL,
    drawn_at TIMESTAMP WITH TIME ZONE,
    CHECK (
        (status = 'open' AND winning_ticket IS NULL AND winner_user_id IS NULL
            AND drawn_at IS NULL)
        OR (status = 'no_winner' AND winning_ticket IS NULL
            AND winner_user_id IS NULL AND drawn_at IS NOT NULL)
        -- A deleted winner is represented by a NULL winner_user_id.  Keep the
        -- round's winning ticket and audit timestamp instead of making a
        -- privacy deletion fail its foreign-key cleanup.
        OR (status = 'drawn' AND winning_ticket IS NOT NULL
            AND drawn_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS lottery_tickets (
    id BIGSERIAL PRIMARY KEY,
    round_start TIMESTAMP WITH TIME ZONE NOT NULL
        REFERENCES lottery_rounds(round_start) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id)
        ON DELETE CASCADE,
    ticket_digits CHAR(6) NOT NULL CHECK (ticket_digits ~ '^[0-9]{6}$'),
    purchased_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    UNIQUE (round_start, ticket_digits)
);

CREATE INDEX IF NOT EXISTS lottery_tickets_user_round_idx
    ON lottery_tickets (user_id, round_start);
