-- Pending submissions do not have a durable media URL yet.  Their source of
-- truth is the attachment on the review message; the CDN URL is written only
-- after a reviewer approves the submission.
ALTER TABLE video_uploads
    ALTER COLUMN source_url DROP NOT NULL;

ALTER TABLE post_uploads
    ALTER COLUMN source_url DROP NOT NULL;

-- Pending rows from the old implementation may contain the submitter's
-- external URL.  They are not library entries, so clear that value as well.
UPDATE video_uploads SET source_url = NULL WHERE status = 'pending';
UPDATE post_uploads SET source_url = NULL WHERE status = 'pending';

-- Approved library rows must always point to the attachment Fishie sent.
-- ``NOT VALID`` lets existing legacy rows be repaired without blocking the
-- migration, while still enforcing the rule for every future write.
ALTER TABLE video_uploads
    ADD CONSTRAINT video_uploads_approved_source_required
    CHECK (status <> 'approved' OR source_url IS NOT NULL) NOT VALID;

ALTER TABLE post_uploads
    ADD CONSTRAINT post_uploads_approved_source_required
    CHECK (status <> 'approved' OR source_url IS NOT NULL) NOT VALID;

ALTER TABLE video_uploads
    ADD CONSTRAINT video_uploads_approved_source_discord
    CHECK (
        status <> 'approved'
        OR source_url ~ '^https://(cdn\.discordapp\.com|media\.discordapp\.net)/'
    ) NOT VALID;

ALTER TABLE post_uploads
    ADD CONSTRAINT post_uploads_approved_source_discord
    CHECK (
        status <> 'approved'
        OR source_url ~ '^https://(cdn\.discordapp\.com|media\.discordapp\.net)/'
    ) NOT VALID;
