-- Persist the warning text shown when a honeypot channel is configured.

ALTER TABLE honeypot_channels
    ADD COLUMN IF NOT EXISTS message_template TEXT NOT NULL DEFAULT
    'This channel was made to catch people who spam in every channel, if you type here there will be no coming back.';
