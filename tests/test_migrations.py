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
        23,
        24,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
    ]
    assert len({item.checksum for item in migrations}) == len(migrations)
    assert all(len(item.checksum) == 64 for item in migrations)


def test_schema_no_longer_runs_from_bot_startup() -> None:
    source = (
        available_migrations()[0].path.parent / "src" / "core" / "bot.py"
    ).read_text()
    assert "check_migrations" in source
    assert "pool.execute(fp.read())" not in source


def test_video_library_migration_tracks_reviews_and_blocks() -> None:
    migration = next(item for item in available_migrations() if item.version == 23)
    assert migration.name == "video_library"
    assert "CREATE TABLE IF NOT EXISTS video_uploads" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS video_upload_blocks" in migration.sql
    assert "status IN ('pending', 'approved', 'denied')" in migration.sql


def test_reaction_logs_migration_requires_explicit_consent() -> None:
    migration = next(item for item in available_migrations() if item.version == 24)
    assert migration.name == "reaction_logs"
    assert "CREATE TABLE IF NOT EXISTS reaction_tracking" in migration.sql
    assert "enabled BOOLEAN NOT NULL DEFAULT FALSE" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS reaction_logs" in migration.sql
    assert "emoji_name TEXT NOT NULL" in migration.sql
    assert "user_created_at" in migration.sql
    assert "guild_created_at" in migration.sql


def test_connectfour_migration_tracks_completed_games() -> None:
    migration = next(item for item in available_migrations() if item.version == 28)
    assert migration.name == "connectfour"
    assert "CREATE TABLE IF NOT EXISTS connectfour_games" in migration.sql
    assert "player_yellow_id" in migration.sql
    assert "player_red_id" in migration.sql
    assert "against_bot BOOLEAN" in migration.sql
    assert "bot_difficulty" in migration.sql


def test_solo_minigame_migration_tracks_2048_and_wordle() -> None:
    migration = next(item for item in available_migrations() if item.version == 29)
    assert migration.name == "solo_minigames"
    assert "wordle_hard_mode" in migration.sql
    assert "wordle_colourblind_mode" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS game_2048_stats" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS wordle_games" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS wordle_stats" in migration.sql


def test_connectfour_moves_migration_tracks_history() -> None:
    migration = next(item for item in available_migrations() if item.version == 30)
    assert migration.name == "connectfour_moves"
    assert "move_count INTEGER" in migration.sql
    assert "move_history JSONB" in migration.sql


def test_2048_moves_migration_tracks_game_history() -> None:
    migration = next(item for item in available_migrations() if item.version == 31)
    assert migration.name == "2048_moves"
    assert "CREATE TABLE IF NOT EXISTS game_2048_games" in migration.sql
    assert "move_count INTEGER" in migration.sql
    assert "move_history JSONB" in migration.sql


def test_lightsout_migration_tracks_completed_games() -> None:
    migration = next(item for item in available_migrations() if item.version == 32)
    assert migration.name == "lightsout"
    assert "CREATE TABLE IF NOT EXISTS lightsout_games" in migration.sql
    assert "move_count INTEGER" in migration.sql
    assert "duration_seconds DOUBLE PRECISION" in migration.sql


def test_streak_games_migration_tracks_personal_bests() -> None:
    migration = next(item for item in available_migrations() if item.version == 33)
    assert migration.name == "streak_games"
    assert "CREATE TABLE IF NOT EXISTS streak_game_stats" in migration.sql
    assert "higher_or_lower" in migration.sql
    assert "heads_or_tails" in migration.sql
    assert "highest_streak" in migration.sql


def test_privacy_migration_preserves_existing_data() -> None:
    migration = next(item for item in available_migrations() if item.version == 10)
    assert migration.name == "user_privacy_settings"
    assert "DELETE " not in migration.sql.upper()
    assert "tracking_enabled" in migration.sql
    assert "history_public" in migration.sql
    assert "first_use_notice_shown" in migration.sql


def test_private_history_defaults_migration_is_idempotent() -> None:
    migration = next(item for item in available_migrations() if item.version == 34)
    assert migration.name == "private_history_defaults"
    assert "ALTER COLUMN history_public SET DEFAULT FALSE" in migration.sql
    assert "SET history_public = FALSE" in migration.sql


def test_game_history_default_is_public() -> None:
    migration = next(item for item in available_migrations() if item.version == 36)
    assert migration.name == "game_history_public_default"
    assert "ALTER COLUMN game_history_public SET DEFAULT TRUE" in migration.sql
    assert "UPDATE user_settings" not in migration.sql


def test_tracking_consent_requires_explicit_non_game_consent() -> None:
    migration = next(item for item in available_migrations() if item.version == 37)
    assert migration.name == "tracking_consent"
    assert "tracking_consent BOOLEAN NOT NULL DEFAULT FALSE" in migration.sql
    assert "game_history_public" not in migration.sql


def test_tracking_consent_backfill_grandfathers_prior_history_commands() -> None:
    migration = next(item for item in available_migrations() if item.version == 38)
    assert migration.name == "tracking_consent_backfill"
    assert "FROM command_logs" in migration.sql
    assert "SET tracking_consent = TRUE" in migration.sql
    assert "'stats command'" not in migration.sql
    assert "'emoji stats'" not in migration.sql
    assert "'activity'" not in migration.sql
    assert "'snipe'" not in migration.sql
    assert "history_public = TRUE" not in migration.sql


def test_tracking_history_backfill_makes_prior_users_public() -> None:
    migration = next(item for item in available_migrations() if item.version == 39)
    assert migration.name == "tracking_history_public_backfill"
    assert "FROM command_logs" in migration.sql
    assert "tracking_consent = TRUE" in migration.sql
    assert "history_public = TRUE" in migration.sql
    assert "'stats command'" not in migration.sql
    assert "'emoji stats'" not in migration.sql
    assert "'activity'" not in migration.sql
    assert "'snipe'" not in migration.sql


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
