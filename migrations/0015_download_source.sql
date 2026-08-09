ALTER TABLE download_stats
    ADD COLUMN IF NOT EXISTS auto_download BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE download_stats
    DROP CONSTRAINT IF EXISTS download_stats_pkey;

ALTER TABLE download_stats
    ADD CONSTRAINT download_stats_pkey PRIMARY KEY (user_id, site, auto_download);
