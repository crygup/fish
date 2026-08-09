CREATE TABLE IF NOT EXISTS minigame_stats (
    game TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    difficulty TEXT NOT NULL,
    wins BIGINT NOT NULL DEFAULT 0 CHECK (wins >= 0),
    fails BIGINT NOT NULL DEFAULT 0 CHECK (fails >= 0),
    PRIMARY KEY (game, user_id, difficulty)
);

CREATE INDEX IF NOT EXISTS minigame_stats_game_idx
    ON minigame_stats (game, difficulty, wins DESC);
