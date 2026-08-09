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
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
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


def test_download_stats_migration_stores_site_totals_only() -> None:
    migration = next(item for item in available_migrations() if item.version == 14)
    assert migration.name == "download_stats"
    assert "CREATE TABLE IF NOT EXISTS download_stats" in migration.sql
    assert "site TEXT" in migration.sql
    assert "url" not in migration.sql.lower()


def test_download_source_migration_separates_auto_downloads() -> None:
    migration = next(item for item in available_migrations() if item.version == 15)
    assert migration.name == "download_source"
    assert "auto_download BOOLEAN" in migration.sql
    assert "PRIMARY KEY (user_id, site, auto_download)" in migration.sql


def test_tictactoe_migration_tracks_completed_games() -> None:
    migration = next(item for item in available_migrations() if item.version == 16)
    assert migration.name == "tictactoe"
    assert "CREATE TABLE IF NOT EXISTS tictactoe_games" in migration.sql
    assert "against_bot BOOLEAN" in migration.sql
    assert "bot_difficulty" in migration.sql
    assert "winner_id" in migration.sql
    assert "loser_id" in migration.sql


def test_snipe_settings_migration_adds_server_controls() -> None:
    migration = next(item for item in available_migrations() if item.version == 17)
    assert migration.name == "snipe_settings"
    assert "snipe_enabled" in migration.sql
    assert "editsnipe_enabled" in migration.sql


def test_minigame_stats_migration_tracks_wins_and_fails() -> None:
    migration = next(item for item in available_migrations() if item.version == 18)
    assert migration.name == "minigame_stats"
    assert "CREATE TABLE IF NOT EXISTS minigame_stats" in migration.sql
    assert "wins BIGINT" in migration.sql
    assert "fails BIGINT" in migration.sql


def test_download_events_migration_tracks_guild_download_activity() -> None:
    migration = next(item for item in available_migrations() if item.version == 19)
    assert migration.name == "download_events"
    assert "CREATE TABLE IF NOT EXISTS download_events" in migration.sql
    assert "guild_id BIGINT" in migration.sql
    assert "auto_download BOOLEAN" in migration.sql


def test_click_migration_tracks_global_user_and_guild_totals() -> None:
    migration = next(item for item in available_migrations() if item.version == 20)
    assert migration.name == "clicks"
    assert "CREATE TABLE IF NOT EXISTS click_totals" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS click_user_totals" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS click_guild_totals" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS click_user_guild_totals" in migration.sql


def test_custom_roles_migration_tracks_roles_and_assignments() -> None:
    migration = next(item for item in available_migrations() if item.version == 21)
    assert migration.name == "custom_roles"
    assert "CREATE TABLE IF NOT EXISTS custom_roles" in migration.sql
    assert "booster_only BOOLEAN" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS custom_role_assignments" in migration.sql
    assert "assigned_by BIGINT" in migration.sql


def test_channel_locks_migration_preserves_previous_overwrites() -> None:
    migration = next(item for item in available_migrations() if item.version == 22)
    assert migration.name == "channel_locks"
    assert "CREATE TABLE IF NOT EXISTS channel_locks" in migration.sql
    assert "had_overwrite BOOLEAN" in migration.sql
    assert "allow_bits BIGINT" in migration.sql
    assert "deny_bits BIGINT" in migration.sql
