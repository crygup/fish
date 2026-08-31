-- Allow wagered Rock Paper Scissors personal bests in the shared streak table.
ALTER TABLE streak_game_stats
    DROP CONSTRAINT IF EXISTS streak_game_stats_game_check;

ALTER TABLE streak_game_stats
    ADD CONSTRAINT streak_game_stats_game_check
    CHECK (
        game IN (
            'higher_or_lower',
            'heads_or_tails',
            'rock_paper_scissors'
        )
    );
