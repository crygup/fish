-- Split legacy game rewards that used a shared difficulty-only source.
--
-- Older releases recorded both Tic-Tac-Toe and Connect Four wins as
-- ``game_reward:game_{easy,normal,hard}``.  Match only an unambiguous game
-- completion for the same winner, difficulty, and near-identical completion
-- timestamp.  Rows without exactly one match are intentionally left alone so
-- this migration cannot invent a game attribution from incomplete history.
WITH legacy AS (
    SELECT
        id,
        user_id,
        created_at,
        substring(source FROM '^game_reward:game_(easy|normal|hard)$') AS difficulty
    FROM currency_transactions
    WHERE source ~ '^game_reward:game_(easy|normal|hard)$'
), game_records AS (
    SELECT
        'tictactoe'::TEXT AS game,
        winner_id AS user_id,
        bot_difficulty AS difficulty,
        finished_at
    FROM tictactoe_games
    WHERE against_bot AND winner_id IS NOT NULL
    UNION ALL
    SELECT
        'connectfour'::TEXT AS game,
        winner_id AS user_id,
        bot_difficulty AS difficulty,
        finished_at
    FROM connectfour_games
    WHERE against_bot AND winner_id IS NOT NULL
), candidates AS (
    SELECT
        legacy.id,
        game_records.game,
        game_records.difficulty,
        count(*) OVER (PARTITION BY legacy.id) AS candidate_count,
        row_number() OVER (
            PARTITION BY legacy.id
            ORDER BY
                abs(EXTRACT(EPOCH FROM (game_records.finished_at - legacy.created_at))),
                game_records.game
        ) AS candidate_rank
    FROM legacy
    JOIN game_records
      ON game_records.user_id = legacy.user_id
     AND game_records.difficulty = legacy.difficulty
     AND abs(EXTRACT(EPOCH FROM (game_records.finished_at - legacy.created_at))) <= 5
), normalized AS (
    SELECT
        id,
        CASE game
            WHEN 'tictactoe' THEN 'game_reward:game_tictactoe_' || difficulty
            WHEN 'connectfour' THEN 'game_reward:game_connectfour_' || difficulty
        END AS source
    FROM candidates
    WHERE candidate_rank = 1 AND candidate_count = 1
)
UPDATE currency_transactions AS transactions
SET source = normalized.source
FROM normalized
WHERE transactions.id = normalized.id
  AND normalized.source IS NOT NULL;
