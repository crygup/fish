-- fishie: no-transaction
-- Build read-path indexes without blocking writes to high-volume log tables.
CREATE INDEX CONCURRENTLY IF NOT EXISTS avatars_user_created_idx ON avatars (user_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS username_logs_user_created_idx ON username_logs (user_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS display_name_logs_user_created_idx ON display_name_logs (user_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS discrim_logs_user_created_idx ON discrim_logs (user_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS nickname_logs_user_guild_created_idx ON nickname_logs (user_id, guild_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS member_join_logs_member_guild_time_idx ON member_join_logs (member_id, guild_id, time DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS command_logs_user_created_idx ON command_logs (user_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS command_logs_guild_created_idx ON command_logs (guild_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS command_logs_guild_command_created_idx ON command_logs (guild_id, command, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS pokemon_solves_user_created_idx ON pokemon_solves (user_id, created_at DESC);
CREATE INDEX CONCURRENTLY IF NOT EXISTS pinboard_pins_guild_idx ON pinboard_pins (guild_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS reminders_user_expires_idx ON reminders ((extra #>> '{args,0}'), expires) WHERE event = 'reminder';
CREATE INDEX CONCURRENTLY IF NOT EXISTS avatars_created_idx ON avatars (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS command_logs_created_idx ON command_logs (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS username_logs_created_idx ON username_logs (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS display_name_logs_created_idx ON display_name_logs (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS discrim_logs_created_idx ON discrim_logs (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS nickname_logs_created_idx ON nickname_logs (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS guild_name_logs_created_idx ON guild_name_logs (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS member_join_logs_time_idx ON member_join_logs (time);
CREATE INDEX CONCURRENTLY IF NOT EXISTS guild_icons_created_idx ON guild_icons (created_at);
CREATE INDEX CONCURRENTLY IF NOT EXISTS guild_avatars_created_idx ON guild_avatars (created_at);
