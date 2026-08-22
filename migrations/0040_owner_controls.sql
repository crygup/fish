-- Persistent owner-managed command and user restrictions.
CREATE TABLE IF NOT EXISTS global_command_disables (
    target TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK (target_type IN ('command', 'cog')),
    disabled_by BIGINT NOT NULL,
    disabled_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS global_user_blocks (
    user_id BIGINT PRIMARY KEY,
    blocked_by BIGINT NOT NULL,
    blocked_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS global_command_disables_type_idx
    ON global_command_disables (target_type);
