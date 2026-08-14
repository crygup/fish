-- Store Connect Four move counts and detailed histories for fastest-win stats.
ALTER TABLE connectfour_games
    ADD COLUMN IF NOT EXISTS move_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS move_history JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE connectfour_games
    DROP CONSTRAINT IF EXISTS connectfour_move_count_check;

ALTER TABLE connectfour_games
    ADD CONSTRAINT connectfour_move_count_check CHECK (move_count >= 0);

