CREATE TABLE IF NOT EXISTS guild_protection_locks (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    role_ids BIGINT[] NOT NULL DEFAULT '{}',
    trigger TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS guild_protection_locks_user_idx
    ON guild_protection_locks (user_id);
