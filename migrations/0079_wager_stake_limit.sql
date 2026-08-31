-- Raise the common wager ceiling to 10,000 Coins.  Keep historical wagers
-- below the current minimum readable; the NOT VALID check still enforces the
-- new range for every new or updated wager.
ALTER TABLE currency_wagers
    DROP CONSTRAINT IF EXISTS currency_wagers_stake_limit_check;

ALTER TABLE currency_wagers
    ADD CONSTRAINT currency_wagers_stake_limit_check
    CHECK (stake BETWEEN 10 AND 10000) NOT VALID;
