-- Keep purchasable titles separate from profile badges.

CREATE TABLE IF NOT EXISTS title_catalog (
    title_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    price BIGINT NOT NULL CHECK (price > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_titles (
    user_id BIGINT NOT NULL,
    title_key TEXT NOT NULL REFERENCES title_catalog(title_key),
    text TEXT NOT NULL,
    purchase_price BIGINT NOT NULL CHECK (purchase_price > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    purchased_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, title_key)
);

CREATE INDEX IF NOT EXISTS user_titles_active_idx
    ON user_titles (user_id, purchased_at DESC)
    WHERE active;

INSERT INTO title_catalog (title_key, display_name, price)
VALUES
    ('custom_title', 'Custom Title', 10000000),
    ('fishie', 'Fishie', 1000000),
    ('dr_pepper', 'Dr Pepper Connoisseur', 670000),
    ('monarch', 'Monarch', 500000),
    ('mudae_enjoyer', 'Mudae Enjoyer', 200000),
    ('epic', 'Epic', 100000),
    ('six_seven', 'Six Seven', 67000),
    ('cool', 'Cool', 50000),
    ('bot', 'Bot', 15000),
    ('fan', 'Fan', 10000),
    ('player', 'Player', 5000)
ON CONFLICT (title_key) DO UPDATE
SET display_name = EXCLUDED.display_name,
    price = EXCLUDED.price,
    enabled = TRUE;

-- 0067 temporarily placed the title offers in the badge catalog.  Keep those
-- historical rows for migration/audit purposes, but make sure they can no
-- longer be returned or purchased as profile badges.
UPDATE badge_catalog
SET enabled = FALSE
WHERE badge_key IN (
    'purchase:custom_title', 'purchase:fishie', 'purchase:dr_pepper',
    'purchase:monarch', 'purchase:mudae_enjoyer', 'purchase:epic',
    'purchase:six_seven', 'purchase:cool', 'purchase:bot',
    'purchase:fan', 'purchase:player'
);

-- 0067 briefly represented titles as purchased badges. Move those rows to
-- the dedicated title table if that migration was already deployed.
INSERT INTO user_titles (
    user_id, title_key, text, purchase_price, active, purchased_at
)
SELECT
    badge.user_id,
    REPLACE(badge.catalog_key, 'purchase:', ''),
    COALESCE(NULLIF(badge.text, ''), catalog.display_name),
    CASE
        WHEN badge.purchase_price > 0 THEN badge.purchase_price
        ELSE catalog.price
    END,
    badge.active,
    COALESCE(badge.created_at, now())
FROM user_badges AS badge
JOIN title_catalog AS catalog
  ON catalog.title_key = REPLACE(badge.catalog_key, 'purchase:', '')
WHERE badge.badge_source = 'purchase'
  AND badge.catalog_key LIKE 'purchase:%'
  AND badge.catalog_key <> 'purchase:custom'
ON CONFLICT (user_id, title_key) DO UPDATE
SET text = EXCLUDED.text,
    purchase_price = EXCLUDED.purchase_price,
    active = EXCLUDED.active,
    purchased_at = EXCLUDED.purchased_at;

UPDATE user_badges
SET active = FALSE,
    revoked_at = COALESCE(revoked_at, now()),
    revocation_reason = 'migrated to user_titles'
WHERE badge_source = 'purchase'
  AND catalog_key LIKE 'purchase:%'
  AND catalog_key <> 'purchase:custom'
  AND catalog_key IN (
      'purchase:custom_title', 'purchase:fishie', 'purchase:dr_pepper',
      'purchase:monarch', 'purchase:mudae_enjoyer', 'purchase:epic',
      'purchase:six_seven', 'purchase:cool', 'purchase:bot',
      'purchase:fan', 'purchase:player'
  );
