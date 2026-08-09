CREATE TABLE IF NOT EXISTS tictactoe_games (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    player_x_id BIGINT NOT NULL,
    player_o_id BIGINT NOT NULL,
    winner_id BIGINT,
    loser_id BIGINT,
    against_bot BOOLEAN NOT NULL DEFAULT FALSE,
    bot_difficulty TEXT,
    started_by_id BIGINT NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    finished_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT tictactoe_winner_player_check
        CHECK (winner_id IS NULL OR winner_id IN (player_x_id, player_o_id)),
    CONSTRAINT tictactoe_loser_player_check
        CHECK (loser_id IS NULL OR loser_id IN (player_x_id, player_o_id)),
    CONSTRAINT tictactoe_winner_loser_check
        CHECK (winner_id IS NULL OR loser_id IS NULL OR winner_id <> loser_id),
    CONSTRAINT tictactoe_bot_difficulty_check
        CHECK (
            (against_bot AND bot_difficulty IN ('easy', 'normal', 'hard'))
            OR (NOT against_bot AND bot_difficulty IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS tictactoe_games_player_x_idx
    ON tictactoe_games (player_x_id);
CREATE INDEX IF NOT EXISTS tictactoe_games_player_o_idx
    ON tictactoe_games (player_o_id);
CREATE INDEX IF NOT EXISTS tictactoe_games_winner_idx
    ON tictactoe_games (winner_id);
CREATE INDEX IF NOT EXISTS tictactoe_games_loser_idx
    ON tictactoe_games (loser_id);
