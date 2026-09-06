-- Kept independently from birthdays so clearing a birthday cannot reset eligibility.
CREATE TABLE IF NOT EXISTS birthday_rewards (
    user_id BIGINT PRIMARY KEY,
    last_awarded_on DATE NOT NULL
);
