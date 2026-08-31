-- Persist the short description shown on a user's generated profile card.
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id BIGINT PRIMARY KEY,
    description TEXT,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT user_profiles_description_length
        CHECK (description IS NULL OR char_length(description) <= 500)
);
