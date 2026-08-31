-- User-purchased emojis used by Sea Animal Race.
-- Unlike badges, the catalog is open-ended: Unicode sequences and custom
-- emojis are recorded on the ownership row so a purchase remains stable even
-- if the shop's sample list changes later.

CREATE TABLE IF NOT EXISTS user_racing_emojis (
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id),
    emoji_key TEXT NOT NULL,
    emoji_name TEXT NOT NULL,
    emoji_id BIGINT,
    unicode BOOLEAN NOT NULL DEFAULT TRUE,
    animated BOOLEAN NOT NULL DEFAULT FALSE,
    display TEXT NOT NULL,
    category TEXT NOT NULL,
    purchase_price BIGINT NOT NULL CHECK (purchase_price > 0),
    purchase_guild_id BIGINT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    equipped BOOLEAN NOT NULL DEFAULT FALSE,
    purchased_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, emoji_key)
);

CREATE INDEX IF NOT EXISTS user_racing_emojis_active_idx
    ON user_racing_emojis (user_id, purchased_at DESC)
    WHERE active;

CREATE UNIQUE INDEX IF NOT EXISTS user_racing_emojis_one_equipped_idx
    ON user_racing_emojis (user_id)
    WHERE active AND equipped;

