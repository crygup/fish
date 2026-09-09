-- Privacy archives are never read by history, export, search or profile queries.
CREATE TABLE privacy_deletions (
    id bigserial PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('user', 'guild')),
    subject_id bigint NOT NULL,
    full_account boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '31 days',
    restored_at timestamptz
);
CREATE INDEX privacy_deletions_subject_idx ON privacy_deletions(scope, subject_id);
CREATE INDEX privacy_deletions_expiry_idx ON privacy_deletions(expires_at);

CREATE TABLE privacy_deleted_rows (
    id bigserial PRIMARY KEY,
    deletion_id bigint NOT NULL REFERENCES privacy_deletions(id) ON DELETE CASCADE,
    table_name text NOT NULL,
    identity_data jsonb NOT NULL,
    before_data jsonb NOT NULL,
    after_data jsonb
);
CREATE INDEX privacy_deleted_rows_deletion_idx ON privacy_deleted_rows(deletion_id);
CREATE INDEX privacy_deleted_rows_table_idx ON privacy_deleted_rows(table_name);

-- A shared record stays hidden until everyone who deleted it has restored it.
CREATE TABLE privacy_deletion_holds (
    row_id bigint NOT NULL REFERENCES privacy_deleted_rows(id) ON DELETE CASCADE,
    deletion_id bigint NOT NULL REFERENCES privacy_deletions(id) ON DELETE CASCADE,
    PRIMARY KEY (row_id, deletion_id)
);
CREATE INDEX privacy_deletion_holds_deletion_idx ON privacy_deletion_holds(deletion_id);

-- The editable badge file has no database row of its own. Stage its user entry
-- here during erasure so it gets the same retention/restore policy.
CREATE TABLE user_badge_documents (
    user_id bigint PRIMARY KEY,
    document jsonb NOT NULL
);

CREATE FUNCTION privacy_row_mentions(data jsonb, scope text, subject bigint)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM jsonb_each_text(data) pair
        WHERE pair.value = subject::text AND pair.key = ANY (
            CASE WHEN scope = 'guild' THEN ARRAY['guild_id', 'source_guild_id']
            ELSE ARRAY['user_id', 'member_id', 'author_id', 'giver_id', 'receiver_id',
                'requester_id', 'recipient_id', 'user_low_id', 'user_high_id',
                'follower_id', 'followed_id', 'proposer_id', 'claiming_user_id',
                'blocked_by', 'assigned_by', 'created_by', 'locked_by', 'disabled_by',
                'actor_id', 'winner_user_id', 'winner_id', 'loser_id', 'started_by_id',
                'player_x_id', 'player_o_id', 'player_red_id', 'player_yellow_id',
                'target_id', 'source_author_id', 'emoji_set_by', 'configured_by',
                'updated_by', 'uploader_id', 'approved_by', 'denied_by',
                'blocked_uploader_id'] END)
    ) OR (scope = 'user' AND (
        data #>> '{extra,args,0}' = subject::text
        OR COALESCE(data->'user_ids', '[]'::jsonb) @> to_jsonb(ARRAY[subject])
    ));
$$;

CREATE FUNCTION privacy_capture_row() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    batch_id bigint := nullif(current_setting('fishie.deletion_id', true), '')::bigint;
    archived_id bigint;
    identity jsonb;
BEGIN
    IF batch_id IS NULL THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND to_jsonb(OLD) = to_jsonb(NEW) THEN RETURN NEW; END IF;
    -- Updating the temporary file mirror is staging, not a user data change.
    IF TG_TABLE_NAME = 'user_badge_documents' AND TG_OP = 'UPDATE' THEN RETURN NEW; END IF;
    SELECT jsonb_object_agg(a.attname, to_jsonb(OLD)->a.attname) INTO identity
    FROM pg_index i JOIN pg_attribute a
      ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = TG_RELID AND i.indisprimary;

    INSERT INTO privacy_deleted_rows(deletion_id, table_name, identity_data, before_data, after_data)
    VALUES (batch_id, TG_TABLE_NAME, COALESCE(identity, to_jsonb(OLD)), to_jsonb(OLD),
            CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(NEW) ELSE NULL END)
    RETURNING id INTO archived_id;
    INSERT INTO privacy_deletion_holds(row_id, deletion_id)
    SELECT archived_id, d.id FROM privacy_deletions d
    WHERE d.id = batch_id OR (d.full_account AND d.restored_at IS NULL
        AND d.expires_at > now() AND privacy_row_mentions(to_jsonb(OLD), d.scope, d.subject_id));
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$;

-- Capture cascades too. Ordinary retention/maintenance deletes have no session
-- deletion_id and are not archived. Login sessions and anti-abuse tombstones
-- deliberately cannot be restored.
DO $$ DECLARE t record; BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
        AND tablename NOT LIKE 'privacy_%'
        AND tablename NOT IN ('schema_migrations', 'web_sessions', 'reward_cooldowns')
    LOOP
        EXECUTE format('CREATE TRIGGER privacy_archive BEFORE DELETE OR UPDATE ON public.%I
                        FOR EACH ROW EXECUTE FUNCTION privacy_capture_row()', t.tablename);
    END LOOP;
END $$;

CREATE FUNCTION privacy_expire_deletions() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    -- Expiry of ANY owner's request permanently removes the shared snapshot.
    DELETE FROM privacy_deleted_rows r USING privacy_deletion_holds h, privacy_deletions d
    WHERE h.row_id = r.id AND h.deletion_id = d.id AND d.expires_at <= now();
    DELETE FROM privacy_deletions WHERE expires_at <= now();
END;
$$;
