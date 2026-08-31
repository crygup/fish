-- Per-user badge display order.  Keys are stable badge_key values (or the
-- deterministic ``json:<user>:<hash>`` keys used for legacy user_flags
-- entries).  Missing keys are ignored when a profile is rendered and active
-- badges not listed here are appended in their normal order.
CREATE TABLE IF NOT EXISTS user_badge_orders (
    user_id BIGINT PRIMARY KEY,
    badge_keys TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_badge_orders_updated_idx
    ON user_badge_orders (updated_at DESC);
