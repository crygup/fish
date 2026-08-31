-- Expand the Coins shop with purchasable titles and Unicode emoji badges.
-- Existing purchase:amulet rows remain valid; the shop now presents that
-- badge as Hamsa while retaining its original stable key for compatibility.

INSERT INTO badge_catalog (
    badge_key, category, display_name, emoji_name, is_custom, unicode, price
)
VALUES
    ('purchase:custom_title', 'purchase', 'Custom Title', '', FALSE, TRUE, 10000000),
    ('purchase:fishie', 'purchase', 'Fishie', '', FALSE, TRUE, 1000000),
    ('purchase:dr_pepper', 'purchase', 'Dr Pepper Connoisseur', '', FALSE, TRUE, 670000),
    ('purchase:mudae_enjoyer', 'purchase', 'Mudae Enjoyer', '', FALSE, TRUE, 200000),
    ('purchase:epic', 'purchase', 'Epic', '', FALSE, TRUE, 100000),
    ('purchase:six_seven', 'purchase', 'Six Seven', '', FALSE, TRUE, 67000),
    ('purchase:cool', 'purchase', 'Cool', '', FALSE, TRUE, 50000),
    ('purchase:bot', 'purchase', 'Bot', '', FALSE, TRUE, 15000),
    ('purchase:fan', 'purchase', 'Fan', '', FALSE, TRUE, 10000),
    ('purchase:player', 'purchase', 'Player', '', FALSE, TRUE, 5000),
    ('purchase:smiling_imp', 'purchase', 'Smiling Imp', '😈', FALSE, TRUE, 20000),
    ('purchase:imp', 'purchase', 'Imp', '👿', FALSE, TRUE, 20000),
    ('purchase:seal', 'purchase', 'Seal', '🦭', FALSE, TRUE, 15000),
    ('purchase:cat', 'purchase', 'Cat', '🐱', FALSE, TRUE, 15000),
    ('purchase:dog', 'purchase', 'Dog', '🐶', FALSE, TRUE, 15000),
    ('purchase:mouse', 'purchase', 'Mouse', '🐭', FALSE, TRUE, 15000),
    ('purchase:hamster', 'purchase', 'Hamster', '🐹', FALSE, TRUE, 15000),
    ('purchase:rabbit', 'purchase', 'Rabbit', '🐰', FALSE, TRUE, 15000),
    ('purchase:fox', 'purchase', 'Fox', '🦊', FALSE, TRUE, 15000)
ON CONFLICT (badge_key) DO UPDATE
SET category = EXCLUDED.category,
    display_name = EXCLUDED.display_name,
    emoji_name = EXCLUDED.emoji_name,
    is_custom = EXCLUDED.is_custom,
    unicode = EXCLUDED.unicode,
    price = EXCLUDED.price,
    enabled = TRUE;

UPDATE badge_catalog
SET display_name = 'Hamsa'
WHERE badge_key = 'purchase:amulet';

UPDATE badge_catalog
SET display_name = '#1 Connect4 Hard Mode Winner'
WHERE badge_key = 'stat:connectfour_hard_winner';
