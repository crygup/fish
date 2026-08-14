-- Completed Connect Four games and difficulty-specific leaderboards.
CREATE TABLE IF NOT EXISTS connectfour_games (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    player_yellow_id BIGINT NOT NULL,
    player_red_id BIGINT NOT NULL,
    winner_id BIGINT,
    loser_id BIGINT,
    against_bot BOOLEAN NOT NULL DEFAULT FALSE,
    bot_difficulty TEXT,
    started_by_id BIGINT NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    finished_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT connectfour_winner_player_check
        CHECK (
            winner_id IS NULL
            OR winner_id IN (player_yellow_id, player_red_id)
        ),
    CONSTRAINT connectfour_loser_player_check
        CHECK (
            loser_id IS NULL
            OR loser_id IN (player_yellow_id, player_red_id)
        ),
    CONSTRAINT connectfour_winner_loser_check
        CHECK (
            winner_id IS NULL
            OR loser_id IS NULL
            OR winner_id <> loser_id
        ),
    CONSTRAINT connectfour_bot_difficulty_check
        CHECK (
            (against_bot AND bot_difficulty IN ('easy', 'normal', 'hard'))
            OR (NOT against_bot AND bot_difficulty IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS connectfour_games_player_yellow_idx
    ON connectfour_games (player_yellow_id);
CREATE INDEX IF NOT EXISTS connectfour_games_player_red_idx
    ON connectfour_games (player_red_id);
CREATE INDEX IF NOT EXISTS connectfour_games_winner_idx
    ON connectfour_games (winner_id);
CREATE INDEX IF NOT EXISTS connectfour_games_loser_idx
    ON connectfour_games (loser_id);
CREATE INDEX IF NOT EXISTS connectfour_games_difficulty_winner_idx
    ON connectfour_games (bot_difficulty, winner_id)
    WHERE against_bot;
