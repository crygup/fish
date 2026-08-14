-- Users with an existing tracking-history command are grandfathered into the
-- consent flow. Public statistics and independently controlled features such
-- as activity, command/download/emoji stats, corn, snipe, and editsnipe do not
-- count as consent. Preserve each user's existing history visibility choice.
WITH normalized_commands AS (
    SELECT DISTINCT user_id, lower(btrim(command)) AS command
    FROM command_logs
), prior_tracking_users AS (
    SELECT DISTINCT user_id
    FROM normalized_commands
    WHERE command IN (
        'avatars',
        'avatars server',
        'avatarhistory',
        'avatarhistory server',
        'discrims',
        'names',
        'nicknames',
        'servertags',
        'servernames',
        'icons',
        'status',
        'statuscalendar',
        'statuses',
        'statushistory',
        'user avatar history',
        'user avatar list',
        'user usernames',
        'user names',
        'user nicknames',
        'user discrims',
        'user servertags',
        'user joins',
        'user stats',
        'user status-calendar',
        'tag stats',
        'uptime',
        'joins',
        'joins leaderboard',
        'joins global',
        'stats joins',
        'stats join'
    )
)
INSERT INTO user_settings (user_id, tracking_consent)
SELECT user_id, TRUE
FROM prior_tracking_users
ON CONFLICT (user_id) DO UPDATE
SET tracking_consent = TRUE;
