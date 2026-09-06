CREATE TABLE IF NOT EXISTS user_birthdays (
    user_id BIGINT PRIMARY KEY,
    month SMALLINT NOT NULL CHECK (month BETWEEN 1 AND 12),
    day SMALLINT NOT NULL CHECK (day BETWEEN 1 AND 31),
    year SMALLINT CHECK (year BETWEEN 1 AND 9999),
    CHECK (EXTRACT(MONTH FROM make_date(COALESCE(year, 2000), month, day)) = month)
);
