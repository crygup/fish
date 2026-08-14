-- Track completed Lights Out puzzles for move and completion-time statistics.
CREATE TABLE IF NOT EXISTS lightsout_games (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    move_count INTEGER NOT NULL CHECK (move_count >= 0),
    duration_seconds DOUBLE PRECISION NOT NULL CHECK (duration_seconds >= 0),
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lightsout_games_user_idx
    ON lightsout_games (user_id, finished_at DESC);

CREATE INDEX IF NOT EXISTS lightsout_games_fastest_idx
    ON lightsout_games (duration_seconds, move_count, finished_at);

CREATE INDEX IF NOT EXISTS lightsout_games_fewest_moves_idx
    ON lightsout_games (move_count, duration_seconds, finished_at);
