-- Solo 2048 and Wordle statistics/settings.
ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS wordle_hard_mode BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS wordle_colourblind_mode BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS game_2048_stats (
    user_id BIGINT PRIMARY KEY,
    high_score BIGINT NOT NULL DEFAULT 0 CHECK (high_score >= 0),
    total_playtime_seconds BIGINT NOT NULL DEFAULT 0 CHECK (total_playtime_seconds >= 0),
    games_completed BIGINT NOT NULL DEFAULT 0 CHECK (games_completed >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wordle_games (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    answer TEXT NOT NULL,
    solved BOOLEAN NOT NULL,
    attempts SMALLINT NOT NULL CHECK (attempts BETWEEN 0 AND 6),
    hard_mode BOOLEAN NOT NULL DEFAULT FALSE,
    colourblind_mode BOOLEAN NOT NULL DEFAULT FALSE,
    duration_seconds BIGINT NOT NULL DEFAULT 0 CHECK (duration_seconds >= 0),
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wordle_stats (
    user_id BIGINT PRIMARY KEY,
    wins BIGINT NOT NULL DEFAULT 0 CHECK (wins >= 0),
    losses BIGINT NOT NULL DEFAULT 0 CHECK (losses >= 0),
    total_playtime_seconds BIGINT NOT NULL DEFAULT 0 CHECK (total_playtime_seconds >= 0),
    attempt_1 BIGINT NOT NULL DEFAULT 0 CHECK (attempt_1 >= 0),
    attempt_2 BIGINT NOT NULL DEFAULT 0 CHECK (attempt_2 >= 0),
    attempt_3 BIGINT NOT NULL DEFAULT 0 CHECK (attempt_3 >= 0),
    attempt_4 BIGINT NOT NULL DEFAULT 0 CHECK (attempt_4 >= 0),
    attempt_5 BIGINT NOT NULL DEFAULT 0 CHECK (attempt_5 >= 0),
    attempt_6 BIGINT NOT NULL DEFAULT 0 CHECK (attempt_6 >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wordle_games_user_idx
    ON wordle_games (user_id, finished_at DESC);
CREATE INDEX IF NOT EXISTS wordle_games_guild_idx
    ON wordle_games (guild_id, finished_at DESC);
