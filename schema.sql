CREATE TABLE IF NOT EXISTS accounts (
    user_id BIGINT NOT NULL,
    last_fm TEXT,
    roblox TEXT,
    steam TEXT,
    PRIMARY KEY (user_id)
);