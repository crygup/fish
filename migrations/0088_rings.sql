-- Purchasable rings and stackable user ring inventories.

CREATE TABLE IF NOT EXISTS ring_catalog (
    ring_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    emoji_name TEXT NOT NULL,
    emoji_id BIGINT,
    unicode BOOLEAN NOT NULL DEFAULT TRUE,
    animated BOOLEAN NOT NULL DEFAULT FALSE,
    display TEXT NOT NULL,
    price BIGINT NOT NULL CHECK (price > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_rings (
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id),
    ring_key TEXT NOT NULL REFERENCES ring_catalog(ring_key),
    quantity BIGINT NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    equipped_count BIGINT NOT NULL DEFAULT 0
        CHECK (
            equipped_count IN (0, 1)
            AND equipped_count <= quantity
        ),
    purchased_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, ring_key)
);

CREATE INDEX IF NOT EXISTS user_rings_inventory_idx
    ON user_rings (user_id, updated_at DESC)
    WHERE quantity > 0;

CREATE UNIQUE INDEX IF NOT EXISTS user_rings_one_equipped_idx
    ON user_rings (user_id)
    WHERE equipped_count > 0;

INSERT INTO ring_catalog(
    ring_key, display_name, emoji_name, emoji_id, unicode, animated,
    display, price, enabled
)
VALUES
    ('basic', 'Basic', '💍', NULL, TRUE, FALSE, '💍', 50000, TRUE),
    ('runalds', 'Runalds', 'Runalds', 1545461490948116622, FALSE, FALSE,
        '<:Runalds:1545461490948116622>', 100000, TRUE),
    ('kjaros', 'Kjaros', 'Kjaros', 1545461494613938320, FALSE, FALSE,
        '<:Kjaros:1545461494613938320>', 100000, TRUE),
    ('singularity', 'Singularity', 'Singularity', 1545461498271367199, FALSE, FALSE,
        '<:Singularity:1545461498271367199>', 75000, TRUE)
ON CONFLICT (ring_key) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    emoji_name = EXCLUDED.emoji_name,
    emoji_id = EXCLUDED.emoji_id,
    unicode = EXCLUDED.unicode,
    animated = EXCLUDED.animated,
    display = EXCLUDED.display,
    price = EXCLUDED.price,
    enabled = EXCLUDED.enabled;
