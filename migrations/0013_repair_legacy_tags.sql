-- Older Fishie versions created a smaller tags table before aliases and
-- ownership state were added. CREATE TABLE IF NOT EXISTS does not alter that
-- table, so add the missing columns and a stable ID sequence in place.
CREATE TABLE IF NOT EXISTS tags (
    id BIGINT,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    author_id BIGINT NOT NULL,
    claimed BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    claimed_at TIMESTAMP WITH TIME ZONE,
    uses BIGINT NOT NULL DEFAULT 0 CHECK (uses >= 0)
);

ALTER TABLE tags
    ADD COLUMN IF NOT EXISTS id BIGINT,
    ADD COLUMN IF NOT EXISTS aliases TEXT[] DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS claimed BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS uses BIGINT DEFAULT 0;

CREATE SEQUENCE IF NOT EXISTS tags_id_seq;

ALTER SEQUENCE tags_id_seq OWNED BY tags.id;

ALTER TABLE tags
    ALTER COLUMN id SET DEFAULT nextval('tags_id_seq'),
    ALTER COLUMN aliases SET DEFAULT '{}',
    ALTER COLUMN claimed SET DEFAULT TRUE,
    ALTER COLUMN uses SET DEFAULT 0;

UPDATE tags
SET id = nextval('tags_id_seq')
WHERE id IS NULL;

UPDATE tags
SET aliases = '{}'
WHERE aliases IS NULL;

UPDATE tags
SET claimed = TRUE
WHERE claimed IS NULL;

UPDATE tags
SET claimed_at = created_at
WHERE claimed AND claimed_at IS NULL;

UPDATE tags
SET uses = 0
WHERE uses IS NULL;

SELECT setval(
    'tags_id_seq',
    COALESCE((SELECT MAX(id) + 1 FROM tags), 1),
    false
);

ALTER TABLE tags
    ALTER COLUMN id SET NOT NULL,
    ALTER COLUMN aliases SET NOT NULL,
    ALTER COLUMN claimed SET NOT NULL,
    ALTER COLUMN uses SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS tags_id_unique_idx ON tags (id);
