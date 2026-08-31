-- Expand the title shop with categories, updated prices, and JoJo stands.
-- Existing ownership rows keep the price paid at purchase time; these values
-- control the catalog price for future purchases and shop rendering.

ALTER TABLE title_catalog
    ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'Misc titles';

UPDATE title_catalog
SET category = 'Misc titles'
WHERE category IS NULL OR btrim(category) = '';

INSERT INTO title_catalog (title_key, display_name, price, category)
VALUES
    ('custom_title', 'Custom Title', 1000000000, 'Misc titles'),
    ('fishie', 'Fishie', 1000000, 'Misc titles'),
    ('dr_pepper', 'Dr Pepper Connoisseur', 670000, 'Misc titles'),
    ('monarch', 'Monarch', 500000, 'Misc titles'),
    ('mudae_enjoyer', 'Mudae Enjoyer', 200000, 'Misc titles'),
    ('epic', 'Epic', 100000, 'Misc titles'),
    ('six_seven', 'Six Seven', 67000, 'Misc titles'),
    ('cool', 'Cool', 50000, 'Misc titles'),
    ('bot', 'Bot', 15000, 'Misc titles'),
    ('fan', 'Fan', 10000, 'Misc titles'),
    ('player', 'Player', 5000, 'Misc titles'),
    ('star_platinum', 'Star Platinum', 5000000, 'JoJo Stands'),
    ('magicians_red', 'Magician''s Red', 5000000, 'JoJo Stands'),
    ('hermit_purple', 'Hermit Purple', 5000000, 'JoJo Stands'),
    ('hierophant_green', 'Hierophant Green', 5000000, 'JoJo Stands'),
    ('silver_chariot', 'Silver Chariot', 5000000, 'JoJo Stands'),
    ('the_fool', 'The Fool', 500000, 'JoJo Stands'),
    ('the_world', 'The World', 5000000, 'JoJo Stands'),
    ('tower_of_gray', 'Tower of Gray', 500000, 'JoJo Stands'),
    ('dark_blue_moon', 'Dark Blue Moon', 500000, 'JoJo Stands'),
    ('strength', 'Strength', 500000, 'JoJo Stands'),
    ('ebony_devil', 'Ebony Devil', 500000, 'JoJo Stands'),
    ('yellow_temperance', 'Yellow Temperance', 500000, 'JoJo Stands'),
    ('hanged_man', 'Hanged Man', 500000, 'JoJo Stands'),
    ('emperor', 'Emperor', 500000, 'JoJo Stands'),
    ('empress', 'Empress', 500000, 'JoJo Stands'),
    ('wheel_of_fortune', 'Wheel of Fortune', 500000, 'JoJo Stands'),
    ('justice', 'Justice', 500000, 'JoJo Stands'),
    ('lovers', 'Lovers', 500000, 'JoJo Stands'),
    ('sun', 'Sun', 500000, 'JoJo Stands'),
    ('death_thirteen', 'Death Thirteen', 500000, 'JoJo Stands'),
    ('judgement', 'Judgement', 500000, 'JoJo Stands'),
    ('high_priestess', 'High Priestess', 500000, 'JoJo Stands'),
    ('khnum', 'Khnum', 500000, 'JoJo Stands'),
    ('tohth', 'Tohth', 500000, 'JoJo Stands'),
    ('anubis', 'Anubis', 500000, 'JoJo Stands'),
    ('soft_wet', 'Soft & Wet', 5000000, 'JoJo Stands'),
    ('wonder_of_u', 'Wonder of U', 5000000, 'JoJo Stands'),
    ('sticky_fingers', 'Sticky Fingers', 5000000, 'JoJo Stands'),
    ('cheap_trick', 'Cheap Trick', 500000, 'JoJo Stands'),
    ('crazy_diamond', 'Crazy Diamond', 5000000, 'JoJo Stands'),
    ('the_hand', 'The Hand', 5000000, 'JoJo Stands'),
    ('harvest', 'Harvest', 500000, 'JoJo Stands'),
    ('echoes', 'Echoes', 5000000, 'JoJo Stands'),
    ('light_rod', 'Light Rod', 25000000, 'JoJo Stands')
ON CONFLICT (title_key) DO UPDATE
SET display_name = EXCLUDED.display_name,
    price = EXCLUDED.price,
    category = EXCLUDED.category,
    enabled = TRUE;

-- Keep the historical badge rows consistent for installations that deployed
-- the brief period where titles were represented in the badge catalog.
UPDATE badge_catalog
SET price = 10000000000
WHERE badge_key = 'purchase:custom';

UPDATE badge_catalog
SET price = 1000000000
WHERE badge_key = 'purchase:custom_title';
