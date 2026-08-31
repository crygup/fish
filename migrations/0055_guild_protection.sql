CREATE TABLE IF NOT EXISTS guild_protection (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    protection_role_id BIGINT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    response_mode TEXT NOT NULL DEFAULT 'both'
        CHECK (response_mode IN ('warn', 'lock', 'both')),
    allowed_vanity_code TEXT,
    configured_by BIGINT,
    updated_by BIGINT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS guild_protection_triggers (
    guild_id BIGINT NOT NULL REFERENCES guild_protection(guild_id)
        ON DELETE CASCADE,
    trigger TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    threshold INTEGER NOT NULL CHECK (threshold > 0),
    window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, trigger)
);

CREATE TABLE IF NOT EXISTS guild_protection_incidents (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL,
    target_id BIGINT,
    trigger TEXT NOT NULL,
    audit_entry_id BIGINT,
    response_mode TEXT NOT NULL
        CHECK (response_mode IN ('warn', 'lock', 'both')),
    contained BOOLEAN NOT NULL DEFAULT FALSE,
    details TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS guild_protection_incidents_audit_idx
    ON guild_protection_incidents (guild_id, audit_entry_id, trigger)
    WHERE audit_entry_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS guild_protection_incidents_guild_created_idx
    ON guild_protection_incidents (guild_id, created_at DESC);

CREATE INDEX IF NOT EXISTS guild_protection_incidents_actor_created_idx
    ON guild_protection_incidents (actor_id, created_at DESC);
