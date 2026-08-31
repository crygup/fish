-- Raise the per-game wager cap from 100 to 500 Coins.
-- The application validates the same limit before opening a wager; this
-- migration updates existing databases so wagers up to the new cap are
-- accepted by PostgreSQL as well.

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'currency_wagers'::regclass
          AND conname LIKE 'currency_wagers_stake%'
          AND conname <> 'currency_wagers_stake_limit_check'
    LOOP
        EXECUTE format(
            'ALTER TABLE currency_wagers DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'currency_wagers'::regclass
          AND conname = 'currency_wagers_stake_limit_check'
    ) THEN
        ALTER TABLE currency_wagers
            ADD CONSTRAINT currency_wagers_stake_limit_check
            CHECK (stake BETWEEN 1 AND 500);
    END IF;
END $$;
