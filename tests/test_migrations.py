from core.migrations import available_migrations


def test_migrations_are_unique_and_have_content_checksums() -> None:
    migrations = available_migrations()
    assert [item.version for item in migrations] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
    ]
    assert len({item.checksum for item in migrations}) == len(migrations)
    assert all(len(item.checksum) == 64 for item in migrations)


def test_schema_no_longer_runs_from_bot_startup() -> None:
    source = (
        available_migrations()[0].path.parent / "src" / "core" / "bot.py"
    ).read_text()
    assert "check_migrations" in source
    assert "pool.execute(fp.read())" not in source


def test_privacy_migration_preserves_existing_data() -> None:
    migration = next(item for item in available_migrations() if item.version == 10)
    assert migration.name == "user_privacy_settings"
    assert "DELETE " not in migration.sql.upper()
    assert "tracking_enabled" in migration.sql
    assert "history_public" in migration.sql
    assert "first_use_notice_shown" in migration.sql


def test_legacy_tag_repair_migration_adds_missing_columns() -> None:
    migration = next(item for item in available_migrations() if item.version == 13)
    assert migration.name == "repair_legacy_tags"
    assert "ADD COLUMN IF NOT EXISTS aliases" in migration.sql
    assert "ADD COLUMN IF NOT EXISTS claimed" in migration.sql
    assert "tags_id_seq" in migration.sql
