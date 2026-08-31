-- Purchasable profile colours and one equipped colour per user.

CREATE TABLE IF NOT EXISTS color_catalog (
    color_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    hex_value TEXT NOT NULL,
    price BIGINT NOT NULL CHECK (price > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_colors (
    user_id BIGINT NOT NULL REFERENCES currency_wallets(user_id),
    color_key TEXT NOT NULL,
    hex_value TEXT NOT NULL,
    purchase_price BIGINT NOT NULL CHECK (purchase_price > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    equipped BOOLEAN NOT NULL DEFAULT FALSE,
    purchased_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, color_key),
    CONSTRAINT user_colors_catalog_fk
        FOREIGN KEY (color_key) REFERENCES color_catalog(color_key)
);

CREATE INDEX IF NOT EXISTS user_colors_active_idx
    ON user_colors (user_id, purchased_at DESC)
    WHERE active;

CREATE UNIQUE INDEX IF NOT EXISTS user_colors_one_equipped_idx
    ON user_colors (user_id)
    WHERE active AND equipped;

INSERT INTO color_catalog (color_key, display_name, hex_value, price)
VALUES
    ('red', 'Red', '#FF0000', 5000),
    ('orange', 'Orange', '#FFA500', 5000),
    ('yellow', 'Yellow', '#FFD700', 5000),
    ('green', 'Green', '#00FF00', 5000),
    ('blue', 'Blue', '#0000FF', 5000),
    ('purple', 'Purple', '#800080', 5000),
    ('pink', 'Pink', '#FF69B4', 5000),
    ('gray', 'Gray', '#808080', 5000),
    ('white', 'White', '#FFFFFF', 10000),
    ('black', 'Black', '#000000', 10000),
    ('custom', 'Custom', '#000000', 25000)
ON CONFLICT (color_key) DO UPDATE
SET display_name = EXCLUDED.display_name,
    hex_value = EXCLUDED.hex_value,
    price = EXCLUDED.price,
    enabled = TRUE;
