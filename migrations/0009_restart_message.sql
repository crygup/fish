CREATE TABLE IF NOT EXISTS bot_restart_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    requested_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
