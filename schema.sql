CREATE TABLE IF NOT EXISTS accounts (
    user_id BIGINT NOT NULL,
    last_fm TEXT,
    steam TEXT,
    roblox TEXT,
    PRIMARY KEY (user_id)
);