CREATE TABLE IF NOT EXISTS custom_roles (
    guild_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL,
    booster_only BOOLEAN NOT NULL DEFAULT FALSE,
    emoji TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, role_id)
);

CREATE INDEX IF NOT EXISTS custom_roles_guild_idx
    ON custom_roles (guild_id);

CREATE TABLE IF NOT EXISTS custom_role_assignments (
    guild_id BIGINT NOT NULL,
    role_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    assigned_by BIGINT NOT NULL,
    assigned_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, role_id, user_id)
);

CREATE INDEX IF NOT EXISTS custom_role_assignments_user_idx
    ON custom_role_assignments (guild_id, user_id);

CREATE INDEX IF NOT EXISTS custom_role_assignments_role_idx
    ON custom_role_assignments (guild_id, role_id);
