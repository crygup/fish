-- Badge catalog, achievement ownership, and purchasable badge metadata.
-- Existing owner-managed rows remain valid: the new columns all default to
-- the owner/source values and the existing (user_id, badge_key) uniqueness is
-- retained so the catalog can safely update a stat badge in place.

ALTER TABLE user_badges
    ADD COLUMN IF NOT EXISTS badge_source TEXT NOT NULL DEFAULT 'owner'
        CHECK (badge_source IN ('owner', 'stat', 'purchase')),
    ADD COLUMN IF NOT EXISTS catalog_key TEXT,
    ADD COLUMN IF NOT EXISTS purchase_price BIGINT NOT NULL DEFAULT 0
        CHECK (purchase_price >= 0),
    ADD COLUMN IF NOT EXISTS purchase_guild_id BIGINT,
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS refund_amount BIGINT NOT NULL DEFAULT 0
        CHECK (refund_amount >= 0),
    ADD COLUMN IF NOT EXISTS revocation_reason TEXT;

CREATE INDEX IF NOT EXISTS user_badges_active_idx
    ON user_badges (user_id, id DESC)
    WHERE active;

CREATE INDEX IF NOT EXISTS user_badges_catalog_idx
    ON user_badges (catalog_key, active, id DESC);

CREATE TABLE IF NOT EXISTS badge_catalog (
    badge_key TEXT PRIMARY KEY,
    category TEXT NOT NULL CHECK (category IN ('stat', 'purchase')),
    display_name TEXT NOT NULL,
    emoji_name TEXT NOT NULL,
    emoji_id BIGINT,
    is_custom BOOLEAN NOT NULL DEFAULT FALSE,
    unicode BOOLEAN NOT NULL DEFAULT TRUE,
    animated BOOLEAN NOT NULL DEFAULT FALSE,
    price BIGINT CHECK (price IS NULL OR price > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CHECK (category = 'stat' OR price IS NOT NULL)
);

INSERT INTO badge_catalog (
    badge_key, category, display_name, emoji_name, price
)
VALUES
    ('stat:corn_receiver', 'stat', '#1 Corn receiver', '🌽', NULL),
    ('stat:connectfour_hard_winner', 'stat',
        '#1 Connect-4 Hard Mode Winner', '4️⃣', NULL),
    ('stat:command_user', 'stat', '#1 command user', '🏅', NULL),
    ('stat:richest', 'stat', 'Richest', '💰', NULL),
    ('purchase:fish', 'purchase', '🐟', '🐟', 1000000),
    ('purchase:amulet', 'purchase', '🪬', '🪬', 500000),
    ('purchase:flag', 'purchase', 'flag', '🏳️', 10000),
    ('purchase:alien', 'purchase', '👽', '👽', 100000),
    ('purchase:custom', 'purchase', 'custom', 'custom', 1000000)
ON CONFLICT (badge_key) DO UPDATE
SET category = EXCLUDED.category,
    display_name = EXCLUDED.display_name,
    emoji_name = EXCLUDED.emoji_name,
    price = EXCLUDED.price;
