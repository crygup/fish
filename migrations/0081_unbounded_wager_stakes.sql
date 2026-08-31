-- Remove the historical wager ceiling. Wallet balance and payout overflow
-- checks remain the effective safety limits; games now accept any stake of
-- at least 10 Coins.
ALTER TABLE currency_wagers
    DROP CONSTRAINT IF EXISTS currency_wagers_stake_limit_check;

ALTER TABLE currency_wagers
    DROP CONSTRAINT IF EXISTS currency_wagers_stake_check;

ALTER TABLE currency_wagers
    ADD CONSTRAINT currency_wagers_stake_limit_check
    CHECK (stake >= 10) NOT VALID;
