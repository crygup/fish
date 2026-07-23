ALTER TABLE guild_join_logs
    ADD COLUMN IF NOT EXISTS added_by BIGINT;
