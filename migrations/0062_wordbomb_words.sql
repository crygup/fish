-- Owner-managed additions to the Word Bomb dictionary.
CREATE TABLE IF NOT EXISTS wordbomb_custom_words (
    word TEXT PRIMARY KEY,
    added_by BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT wordbomb_custom_words_alpha CHECK (word ~ '^[[:alpha:]]{2,}$')
);

CREATE INDEX IF NOT EXISTS wordbomb_custom_words_added_by_idx
    ON wordbomb_custom_words (added_by, created_at DESC);
