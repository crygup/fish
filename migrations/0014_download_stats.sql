CREATE TABLE IF NOT EXISTS download_stats (
    user_id BIGINT NOT NULL,
    site TEXT NOT NULL CHECK (site <> ''),
    downloads BIGINT NOT NULL DEFAULT 0 CHECK (downloads >= 0),
    first_downloaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_downloaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, site)
);

CREATE INDEX IF NOT EXISTS download_stats_site_idx
    ON download_stats (site, downloads DESC);
