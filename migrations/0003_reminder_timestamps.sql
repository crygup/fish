-- Convert legacy naive UTC reminder timestamps to unambiguous timestamptz values.
ALTER TABLE reminders
    ALTER COLUMN expires TYPE TIMESTAMP WITH TIME ZONE
        USING expires AT TIME ZONE 'UTC',
    ALTER COLUMN created TYPE TIMESTAMP WITH TIME ZONE
        USING created AT TIME ZONE 'UTC',
    ALTER COLUMN created SET DEFAULT now();
