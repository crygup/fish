-- Global, per-user, and per-guild click counters.
CREATE TABLE IF NOT EXISTS click_totals (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    clicks BIGINT NOT NULL DEFAULT 0 CHECK (clicks >= 0)
);

INSERT INTO click_totals (id, clicks)
VALUES (TRUE, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS click_user_totals (
    user_id BIGINT PRIMARY KEY,
    clicks BIGINT NOT NULL DEFAULT 0 CHECK (clicks >= 0)
);

CREATE TABLE IF NOT EXISTS click_guild_totals (
    guild_id BIGINT PRIMARY KEY,
    clicks BIGINT NOT NULL DEFAULT 0 CHECK (clicks >= 0)
);

CREATE TABLE IF NOT EXISTS click_user_guild_totals (
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    clicks BIGINT NOT NULL DEFAULT 0 CHECK (clicks >= 0),
    PRIMARY KEY (user_id, guild_id)
);
