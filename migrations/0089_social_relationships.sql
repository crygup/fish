-- Fishie friends, follows, and marriages.

CREATE TABLE IF NOT EXISTS social_user_settings (
    user_id BIGINT PRIMARY KEY,
    friend_requests_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS social_friend_requests (
    requester_id BIGINT NOT NULL,
    recipient_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (requester_id, recipient_id),
    CHECK (requester_id <> recipient_id)
);

CREATE INDEX IF NOT EXISTS social_friend_requests_recipient_idx
    ON social_friend_requests (recipient_id, created_at DESC);

CREATE TABLE IF NOT EXISTS social_friendships (
    user_low_id BIGINT NOT NULL,
    user_high_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_low_id, user_high_id),
    CHECK (user_low_id < user_high_id)
);

CREATE INDEX IF NOT EXISTS social_friendships_high_idx
    ON social_friendships (user_high_id, created_at DESC);

CREATE TABLE IF NOT EXISTS social_follows (
    follower_id BIGINT NOT NULL,
    followed_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (follower_id, followed_id),
    CHECK (follower_id <> followed_id)
);

CREATE INDEX IF NOT EXISTS social_follows_followed_idx
    ON social_follows (followed_id, created_at DESC);

CREATE TABLE IF NOT EXISTS social_marriages (
    id BIGSERIAL PRIMARY KEY,
    proposer_id BIGINT NOT NULL,
    recipient_id BIGINT NOT NULL,
    ring_key TEXT NOT NULL REFERENCES ring_catalog(ring_key),
    married_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CHECK (proposer_id <> recipient_id)
);

CREATE TABLE IF NOT EXISTS social_marriage_members (
    user_id BIGINT PRIMARY KEY,
    marriage_id BIGINT NOT NULL REFERENCES social_marriages(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS social_marriage_members_marriage_idx
    ON social_marriage_members (marriage_id);

CREATE TABLE IF NOT EXISTS social_marriage_cooldowns (
    user_id BIGINT PRIMARY KEY,
    available_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS social_marriage_cooldowns_available_idx
    ON social_marriage_cooldowns (available_at);
