-- Public library IDs are assigned only when a submission is approved.
-- Submission primary keys remain internal so pending and denied submissions do
-- not consume public slots. Tombstones reserve IDs that were deleted.

ALTER TABLE video_uploads
    ADD COLUMN IF NOT EXISTS library_id BIGINT;

ALTER TABLE post_uploads
    ADD COLUMN IF NOT EXISTS library_id BIGINT;

-- Preserve the IDs already visible for approved entries. Denied and pending
-- rows intentionally remain without a public library ID.
UPDATE video_uploads
SET library_id = id
WHERE status = 'approved' AND library_id IS NULL;

UPDATE post_uploads
SET library_id = id
WHERE status = 'approved' AND library_id IS NULL;

UPDATE video_uploads
SET library_id = NULL
WHERE status <> 'approved';

UPDATE post_uploads
SET library_id = NULL
WHERE status <> 'approved';

CREATE UNIQUE INDEX IF NOT EXISTS video_uploads_library_id_idx
    ON video_uploads (library_id)
    WHERE library_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS post_uploads_library_id_idx
    ON post_uploads (library_id)
    WHERE library_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS video_library_deleted_ids (
    library_id BIGINT PRIMARY KEY,
    deleted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS post_library_deleted_ids (
    library_id BIGINT PRIMARY KEY,
    deleted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
