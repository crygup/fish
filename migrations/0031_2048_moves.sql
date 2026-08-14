-- Store per-game 2048 move history for fastest-score and fastest-goal stats.
CREATE TABLE IF NOT EXISTS game_2048_games (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT,
    channel_id BIGINT,
    score BIGINT NOT NULL DEFAULT 0 CHECK (score >= 0),
    highest_tile BIGINT NOT NULL DEFAULT 0 CHECK (highest_tile >= 0),
    move_count INTEGER NOT NULL DEFAULT 0 CHECK (move_count >= 0),
    move_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    timed_out BOOLEAN NOT NULL DEFAULT FALSE,
    gave_up BOOLEAN NOT NULL DEFAULT FALSE,
    duration_seconds BIGINT NOT NULL DEFAULT 0 CHECK (duration_seconds >= 0),
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS game_2048_games_user_idx
    ON game_2048_games (user_id, finished_at DESC);

