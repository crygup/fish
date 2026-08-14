-- Users who used a history command before the consent prompt existed are
-- treated as already opted in.  Make that existing history public until they
-- change the setting in Fishie's tracking settings.
WITH prior_tracking_users AS (
    SELECT DISTINCT user_id
    FROM command_logs
    WHERE lower(btrim(command)) IN (
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
UPDATE user_settings AS settings
SET tracking_consent = TRUE,
    history_public = TRUE
FROM prior_tracking_users
WHERE settings.user_id = prior_tracking_users.user_id;
