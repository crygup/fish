-- Owner-managed userinfo badges.  Keep a surrogate id so the table can grow
-- to support badge history in the future while the current command UX keeps
-- one active badge per user.
CREATE TABLE IF NOT EXISTS user_badges (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    emoji_name TEXT NOT NULL,
    emoji_id BIGINT,
    is_custom BOOLEAN NOT NULL,
    unicode BOOLEAN NOT NULL DEFAULT FALSE,
    animated BOOLEAN NOT NULL DEFAULT FALSE,
    badge_key TEXT NOT NULL DEFAULT 'custom',
    text TEXT NOT NULL,
    CHECK (
        (is_custom AND emoji_id IS NOT NULL AND NOT unicode)
        OR (NOT is_custom AND emoji_id IS NULL AND unicode)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS user_badges_user_key_idx
    ON user_badges (user_id, badge_key);

CREATE INDEX IF NOT EXISTS user_badges_user_idx
    ON user_badges (user_id, id DESC);

-- Preserve the owner-managed badges that were previously hard-coded in
-- ``utils.vars.USER_FLAGS``.  JSON keeps the animated/multi-Unicode source
-- representation while these rows retain the searchable name and ID fields
-- used by userinfo and the owner commands.
INSERT INTO user_badges (
    user_id, emoji_name, emoji_id, is_custom, unicode, animated, badge_key, text
)
VALUES
    (1247673648626667642, '💫', NULL, FALSE, TRUE, FALSE, 'custom', 'ori'),
    (536939325380755487, 'sac', 1526723466760421406, TRUE, FALSE, FALSE, 'custom', 'Sac'),
    (1471310794648977491, '🦃 🦷 👶', NULL, FALSE, TRUE, FALSE, 'custom', 'Smuckers'),
    (517007731400507414, '😹', NULL, FALSE, TRUE, FALSE, 'custom', 'Aikuri'),
    (301104022830710786, '✨', NULL, FALSE, TRUE, FALSE, 'custom', 'Jane'),
    (766953372309127168, 'drpepper', 1526723524587421778, TRUE, FALSE, TRUE, 'custom', 'z'),
    (639539828400062485, 'jpj', 1132056944308400220, TRUE, FALSE, TRUE, 'custom', 'jpjordon'),
    (117666744021024771, 'bfr', 1132056779279302716, TRUE, FALSE, TRUE, 'custom', 'bfr'),
    (592310159133376512, 'razy', 1132056914365264036, TRUE, FALSE, TRUE, 'custom', 'Razy'),
    (671777334906454026, 'kaylynn', 1132059155272831076, TRUE, FALSE, TRUE, 'custom', 'kay'),
    (809275012980539453, 'regor', 1132163863262019715, TRUE, FALSE, TRUE, 'custom', 'Regor'),
    (780272096688996363, 'kami', 1132058497849237665, TRUE, FALSE, TRUE, 'custom', 'Kami'),
    (466325650710724619, 'lunachup', 1132059704210751579, TRUE, FALSE, TRUE, 'custom', 'Lunachup!'),
    (121738169900204034, '🀄', NULL, FALSE, TRUE, FALSE, 'custom', 'tsugoth'),
    (1384427573492187279, '🍃', NULL, FALSE, TRUE, FALSE, 'custom', 'Skeezr'),
    (117603839858835460, 'spike', 1132060600751624193, TRUE, FALSE, TRUE, 'custom', 'Spike'),
    (364105382261424128, '🐢', NULL, FALSE, TRUE, FALSE, 'custom', 'Eli'),
    (309409635327148033, '🇱🇹', NULL, FALSE, TRUE, FALSE, 'custom', '2co'),
    (725443744060538912, '🏳️‍⚧️', NULL, FALSE, TRUE, FALSE, 'custom', 'cola'),
    (396923243803574282, 'yaz', 1132066955399020564, TRUE, FALSE, TRUE, 'custom', 'Yaz'),
    (349373972103561218, 'leo', 1132065439409770606, TRUE, FALSE, FALSE, 'custom', 'Leonardo'),
    (1034697215991619584, '🤓', NULL, FALSE, TRUE, FALSE, 'custom', 'Leo'),
    (1323759367371231263, 'monark', 1132067164053049475, TRUE, FALSE, FALSE, 'custom', 'Monark'),
    (659385299809599500, 'samir', 1132064265524752414, TRUE, FALSE, FALSE, 'custom', 'Samir'),
    (198145509242699777, '🌭 🍔 🇺🇸', NULL, FALSE, TRUE, FALSE, 'custom', 'Jawn'),
    (829421985846263839, 'sybel', 1210053787385856040, TRUE, FALSE, FALSE, 'custom', 'Sybel'),
    (739641420645924986, '🐝', NULL, FALSE, TRUE, FALSE, 'custom', 'Drew'),
    (805695326392287232, '🥺', NULL, FALSE, TRUE, FALSE, 'custom', 'fart'),
    (299775141041274890, 'mariSparkle', 1390265426008604703, TRUE, FALSE, FALSE, 'custom', 'Tony'),
    (483778672747216916, '🦧', NULL, FALSE, TRUE, FALSE, 'custom', 'Dochi'),
    (137329268907573249, '🦊', NULL, FALSE, TRUE, FALSE, 'custom', 'Starla'),
    (410920877857832961, '🫗 💝 🔮', NULL, FALSE, TRUE, FALSE, 'custom', 'cm')
ON CONFLICT (user_id, badge_key) DO NOTHING;
