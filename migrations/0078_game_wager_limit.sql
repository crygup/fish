-- All wager games accept bids from 10 through 2,000 Coins.  Keep historical
-- sub-10 wagers readable rather than rewriting the accounting ledger; NOT
-- VALID still enforces the range for every new row and every updated row.
ALTER TABLE currency_wagers
    DROP CONSTRAINT IF EXISTS currency_wagers_stake_limit_check;

ALTER TABLE currency_wagers
    ADD CONSTRAINT currency_wagers_stake_limit_check
    CHECK (stake BETWEEN 10 AND 2000) NOT VALID;
