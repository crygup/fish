-- Slots accepts wagers up to 1,000 Coins; other games keep their
-- application-level limits.
ALTER TABLE currency_wagers
    DROP CONSTRAINT IF EXISTS currency_wagers_stake_limit_check;

ALTER TABLE currency_wagers
    ADD CONSTRAINT currency_wagers_stake_limit_check
    CHECK (stake BETWEEN 1 AND 1000);
