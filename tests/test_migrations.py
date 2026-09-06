import pytest

from core.migrations import (
    LEGACY_BASELINE_CHECKSUMS,
    available_migrations,
    check_migrations,
)


def test_migrations_are_unique_and_have_content_checksums() -> None:
    migrations = available_migrations()
    versions = [item.version for item in migrations]
    assert versions[0] == 1
    assert versions == sorted(set(versions))
    # Every numbered SQL file must be discovered; adding a migration should
    # not require duplicating the entire migration history in this test.
    paths = set((migrations[0].path.parent / "migrations").glob("*.sql"))
    assert {item.path for item in migrations[1:]} == paths
    assert all(item.sql.strip() for item in migrations)
    assert len({item.checksum for item in migrations}) == len(migrations)
    assert all(len(item.checksum) == 64 for item in migrations)


def test_schema_no_longer_runs_from_bot_startup() -> None:
    source = (
        available_migrations()[0].path.parent / "src" / "core" / "bot.py"
    ).read_text()
    assert "check_migrations" in source
    assert "pool.execute(fp.read())" not in source


@pytest.mark.asyncio
async def test_known_legacy_baseline_checksum_is_retained_for_existing_databases() -> (
    None
):
    migrations = available_migrations()
    legacy_checksum = next(iter(LEGACY_BASELINE_CHECKSUMS))

    class Connection:
        async def fetchval(self, _query: str):
            return "schema_migrations"

        async def fetch(self, _query: str):
            return [
                {
                    "version": migration.version,
                    "checksum": (
                        legacy_checksum
                        if migration.version == 1
                        else migration.checksum
                    ),
                    "name": migration.name,
                }
                for migration in migrations
            ]

    await check_migrations(Connection())


@pytest.mark.asyncio
async def test_unknown_baseline_checksum_is_rejected() -> None:
    migrations = available_migrations()

    class Connection:
        async def fetchval(self, _query: str):
            return "schema_migrations"

        async def fetch(self, _query: str):
            return [
                {
                    "version": migration.version,
                    "checksum": (
                        "0" * 64 if migration.version == 1 else migration.checksum
                    ),
                    "name": migration.name,
                }
                for migration in migrations
            ]

    with pytest.raises(RuntimeError, match="differs from the applied version"):
        await check_migrations(Connection())


def test_video_library_migration_tracks_reviews_and_blocks() -> None:
    migration = next(item for item in available_migrations() if item.version == 23)
    assert migration.name == "video_library"
    assert "CREATE TABLE IF NOT EXISTS video_uploads" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS video_upload_blocks" in migration.sql
    assert "status IN ('pending', 'approved', 'denied')" in migration.sql


def test_hourly_post_intervals_migration_adds_schedule_columns() -> None:
    migration = next(item for item in available_migrations() if item.version == 51)
    assert migration.name == "hourly_post_intervals"
    assert "interval_minutes" in migration.sql
    assert "next_post_at" in migration.sql


def test_message_boards_migration_tracks_settings_entries_and_blocks() -> None:
    migration = next(item for item in available_migrations() if item.version == 52)
    assert migration.name == "message_boards"
    assert "CREATE TABLE IF NOT EXISTS guild_boards" in migration.sql
    assert "board_type IN ('starboard', 'clownboard')" in migration.sql
    assert "threshold INTEGER NOT NULL DEFAULT 3" in migration.sql
    assert "allow_nsfw BOOLEAN NOT NULL DEFAULT TRUE" in migration.sql
    assert "guild_boards_custom_emoji_unique_idx" in migration.sql
    assert "guild_boards_unicode_emoji_unique_idx" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS guild_board_entries" in migration.sql
    assert "guild_board_entries_message_unique_idx" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS guild_board_blocks" in migration.sql
    assert "target_type IN ('user', 'channel')" in migration.sql


def test_notify_migration_supports_guild_and_dm_follows() -> None:
    migration = next(item for item in available_migrations() if item.version == 83)
    assert migration.name == "notify_subscriptions"
    assert "CREATE TABLE IF NOT EXISTS notify_twitch_follows" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS notify_anime_follows" in migration.sql
    assert "notify_twitch_owner_check" in migration.sql
    assert "notify_anime_owner_check" in migration.sql
    assert "mention_role_id" in migration.sql
    assert "next_airing_at" in migration.sql
    assert "INSERT INTO notify_twitch_follows" in migration.sql


def test_notify_scope_migration_deduplicates_and_uniquifies_follows() -> None:
    migration = next(item for item in available_migrations() if item.version == 84)
    assert migration.name == "notify_one_follow_per_scope"
    assert "row_number() OVER" in migration.sql
    assert "DROP INDEX IF EXISTS notify_anime_guild_key_idx" in migration.sql
    assert "lower(btrim(channel_name))" in migration.sql
    assert "CREATE UNIQUE INDEX notify_twitch_guild_key_idx" in migration.sql
    assert "CREATE UNIQUE INDEX notify_twitch_user_key_idx" in migration.sql
    assert "CREATE UNIQUE INDEX notify_anime_guild_key_idx" in migration.sql
    assert "UPDATE twitch_follows AS legacy" in migration.sql


def test_mudae_series_metadata_migration_adds_stable_ids_and_scrape_tables() -> None:
    migration = next(item for item in available_migrations() if item.version == 85)
    assert migration.name == "mudae_series_metadata"
    assert "mudae_wishes_id_seq" in migration.sql
    assert "mudae_wishes_id_idx" in migration.sql
    assert "bundle_created_at" in migration.sql
    assert "source_guild_id" in migration.sql
    assert "kakera_threshold" in migration.sql
    assert "mudae_auto_scrape_series" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS mudae_series_bundles" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS mudae_series" in migration.sql
    assert "UNIQUE (bundle_id, normalized_name)" in migration.sql


def test_title_shop_expansion_migration_adds_categories_prices_and_stands() -> None:
    migration = next(item for item in available_migrations() if item.version == 86)
    assert migration.name == "title_shop_expansion"
    assert "ADD COLUMN IF NOT EXISTS category" in migration.sql
    assert "('custom_title', 'Custom Title', 1000000000" in migration.sql
    assert "('star_platinum', 'Star Platinum', 5000000" in migration.sql
    assert "('light_rod', 'Light Rod', 25000000" in migration.sql
    assert "SET price = 10000000000" in migration.sql


def test_message_xp_migration_adds_leaderboard_index() -> None:
    migration = next(item for item in available_migrations() if item.version == 54)
    assert migration.name == "message_xp_leaderboard_index"
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS message_xp_leaderboard_idx" in (
        migration.sql
    )
    assert "ON message_xp (xp DESC, user_id ASC)" in migration.sql


def test_badge_shop_expansion_migration_adds_titles_and_emoji_badges() -> None:
    migration = next(item for item in available_migrations() if item.version == 67)
    assert migration.name == "badge_shop_expansion"
    assert "purchase:custom_title" in migration.sql
    assert "purchase:smiling_imp" in migration.sql
    assert "display_name = 'Hamsa'" in migration.sql
    assert "display_name = '#1 Connect4 Hard Mode Winner'" in migration.sql


def test_titles_migration_separates_titles_from_badges() -> None:
    migration = next(item for item in available_migrations() if item.version == 68)
    assert migration.name == "titles"
    assert "CREATE TABLE IF NOT EXISTS title_catalog" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS user_titles" in migration.sql
    assert "UPDATE badge_catalog" in migration.sql
    assert "migrated to user_titles" in migration.sql


def test_title_equipping_migration_adds_single_equipped_title_state() -> None:
    migration = next(item for item in available_migrations() if item.version == 69)
    assert migration.name == "title_equipping"
    assert "ADD COLUMN IF NOT EXISTS equipped BOOLEAN" in migration.sql
    assert "user_titles_one_equipped_idx" in migration.sql
    assert "ORDER BY user_id, purchased_at DESC" in migration.sql


def test_currency_colors_migration_adds_equipped_user_colors() -> None:
    migration = next(item for item in available_migrations() if item.version == 70)
    assert migration.name == "currency_colors"
    assert "CREATE TABLE IF NOT EXISTS color_catalog" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS user_colors" in migration.sql
    assert "user_colors_one_equipped_idx" in migration.sql


def test_guild_protection_migration_adds_settings_triggers_and_incidents() -> None:
    migration = next(item for item in available_migrations() if item.version == 55)
    assert migration.name == "guild_protection"
    assert "CREATE TABLE IF NOT EXISTS guild_protection" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS guild_protection_triggers" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS guild_protection_incidents" in migration.sql
    assert "response_mode IN ('warn', 'lock', 'both')" in migration.sql
    assert "ON DELETE CASCADE" in migration.sql


def test_guild_protection_lock_migration_saves_roles_for_unlocking() -> None:
    migration = next(item for item in available_migrations() if item.version == 56)
    assert migration.name == "guild_protection_locks"
    assert "CREATE TABLE IF NOT EXISTS guild_protection_locks" in migration.sql
    assert "role_ids BIGINT[]" in migration.sql


def test_currency_migration_adds_wallets_claims_caps_and_wagers() -> None:
    migration = next(item for item in available_migrations() if item.version == 57)
    assert migration.name == "currency"
    assert "CREATE TABLE IF NOT EXISTS currency_wallets" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS currency_transactions" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS currency_claims" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS currency_daily_rewards" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS currency_wagers" in migration.sql
    assert "claim_type IN ('daily', 'weekly')" in migration.sql
    assert "stake BETWEEN 1 AND 100" in migration.sql


def test_currency_claim_streak_migration_adds_streak_columns() -> None:
    migration = next(item for item in available_migrations() if item.version == 59)
    assert migration.name == "currency_claim_streaks"
    assert "ADD COLUMN IF NOT EXISTS streak" in migration.sql
    assert "ADD COLUMN IF NOT EXISTS streak_bonus_amount" in migration.sql


def test_mudae_wishes_migration_persists_per_server_filters() -> None:
    migration = next(item for item in available_migrations() if item.version == 60)
    assert migration.name == "mudae_wishes"
    assert "CREATE TABLE IF NOT EXISTS mudae_wishes" in migration.sql
    assert "wish_type IN ('character', 'series', 'kakera')" in migration.sql
    assert "PRIMARY KEY (guild_id, user_id, wish_type, wish_value)" in migration.sql


def test_wordbomb_migration_tracks_wins_and_losses() -> None:
    migration = next(item for item in available_migrations() if item.version == 61)
    assert migration.name == "wordbomb_stats"
    assert "CREATE TABLE IF NOT EXISTS wordbomb_stats" in migration.sql
    assert "wins BIGINT" in migration.sql
    assert "losses BIGINT" in migration.sql


def test_wordbomb_words_migration_tracks_custom_dictionary_entries() -> None:
    migration = next(item for item in available_migrations() if item.version == 62)
    assert migration.name == "wordbomb_words"
    assert "CREATE TABLE IF NOT EXISTS wordbomb_custom_words" in migration.sql
    assert "added_by BIGINT" in migration.sql


def test_badges_migration_adds_catalog_and_purchase_metadata() -> None:
    migration = next(item for item in available_migrations() if item.version == 63)
    assert migration.name == "badges"
    assert "badge_source" in migration.sql
    assert "purchase_price" in migration.sql
    assert "purchase_guild_id" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS badge_catalog" in migration.sql
    assert "stat:corn_receiver" in migration.sql
    assert "purchase:custom" in migration.sql


def test_wager_stake_limit_migration_raises_cap_to_500() -> None:
    migration = next(item for item in available_migrations() if item.version == 65)
    assert migration.name == "wager_stake_limit"
    assert "currency_wagers_stake_limit_check" in migration.sql
    assert "CHECK (stake BETWEEN 1 AND 500)" in migration.sql


def test_slots_wager_limit_migration_raises_database_cap_to_1000() -> None:
    migration = next(item for item in available_migrations() if item.version == 76)
    assert migration.name == "slots_wager_limit"
    assert "DROP CONSTRAINT IF EXISTS currency_wagers_stake_limit_check" in (
        migration.sql
    )
    assert "CHECK (stake BETWEEN 1 AND 1000)" in migration.sql


def test_game_wager_limit_migration_uses_common_game_range() -> None:
    migration = next(item for item in available_migrations() if item.version == 78)
    assert migration.name == "game_wager_limit"
    assert "DROP CONSTRAINT IF EXISTS currency_wagers_stake_limit_check" in (
        migration.sql
    )
    assert "CHECK (stake BETWEEN 10 AND 2000)" in migration.sql


def test_wager_stake_limit_migration_raises_ceiling_to_10000() -> None:
    migration = next(item for item in available_migrations() if item.version == 79)
    assert migration.name == "wager_stake_limit"
    assert "DROP CONSTRAINT IF EXISTS currency_wagers_stake_limit_check" in (
        migration.sql
    )
    assert "CHECK (stake BETWEEN 10 AND 10000)" in migration.sql


def test_legacy_game_rewards_migration_splits_unambiguous_game_sources() -> None:
    migration = next(item for item in available_migrations() if item.version == 82)
    assert migration.name == "normalize_legacy_game_rewards"
    assert "game_reward:game_tictactoe_" in migration.sql
    assert "game_reward:game_connectfour_" in migration.sql
    assert "candidate_count = 1" in migration.sql


def test_currency_gambling_stats_migration_adds_durable_totals() -> None:
    migration = next(item for item in available_migrations() if item.version == 66)
    assert migration.name == "currency_gambling_stats"
    assert "CREATE TABLE IF NOT EXISTS currency_gambling_stats" in migration.sql
    assert "total_wagered BIGINT" in migration.sql
    assert "total_earned BIGINT" in migration.sql
    assert "total_lost BIGINT" in migration.sql
    assert "wins BIGINT" in migration.sql
    assert "losses BIGINT" in migration.sql


def test_badge_orders_migration_adds_display_order_storage() -> None:
    migration = next(item for item in available_migrations() if item.version == 64)
    assert migration.name == "badge_orders"
    assert "CREATE TABLE IF NOT EXISTS user_badge_orders" in migration.sql
    assert "badge_keys TEXT[]" in migration.sql


def test_owner_controls_migration_tracks_global_restrictions() -> None:
    migration = next(item for item in available_migrations() if item.version == 40)
    assert migration.name == "owner_controls"
    assert "CREATE TABLE IF NOT EXISTS global_command_disables" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS global_user_blocks" in migration.sql


def test_video_library_preferences_migration_tracks_personal_filters() -> None:
    migration = next(item for item in available_migrations() if item.version == 41)
    assert migration.name == "video_library_preferences"
    assert "CREATE TABLE IF NOT EXISTS video_aliases" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS video_library_blocks" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS video_library_hides" in migration.sql


def test_post_library_migration_tracks_reviews_and_personal_filters() -> None:
    migration = next(item for item in available_migrations() if item.version == 42)
    assert migration.name == "post_library"
    assert "CREATE TABLE IF NOT EXISTS post_uploads" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS post_upload_blocks" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS post_aliases" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS post_library_blocks" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS post_library_hides" in migration.sql


def test_library_id_migration_reserves_deleted_slots() -> None:
    migration = next(item for item in available_migrations() if item.version == 43)
    assert migration.name == "library_ids"
    assert "ADD COLUMN IF NOT EXISTS library_id BIGINT" in migration.sql
    assert "video_library_deleted_ids" in migration.sql
    assert "post_library_deleted_ids" in migration.sql


def test_reputation_migration_tracks_fishie_and_tatsu_events() -> None:
    migration = next(item for item in available_migrations() if item.version == 44)
    assert migration.name == "reputation"
    assert "CREATE TABLE IF NOT EXISTS reputation_events" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS guild_rep" in migration.sql
    assert "source_message_id" in migration.sql
    assert "period_start" in migration.sql


def test_library_source_urls_are_nullable_until_review_approval() -> None:
    migration = next(item for item in available_migrations() if item.version == 45)
    assert "ALTER TABLE video_uploads" in migration.sql
    assert "ALTER TABLE post_uploads" in migration.sql
    assert "DROP NOT NULL" in migration.sql
    assert "approved_source_required" in migration.sql
    assert "approved_source_discord" in migration.sql


def test_server_media_settings_migration_adds_destinations_and_filters() -> None:
    migration = next(item for item in available_migrations() if item.version == 46)
    assert migration.name == "server_media_settings"
    assert "auto_upload_images" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS guild_hourly_posts" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS guild_hourly_post_blocks" in migration.sql
    assert "images BOOLEAN" in migration.sql
    assert "gifs BOOLEAN" in migration.sql
    assert "videos BOOLEAN" in migration.sql


def test_automation_channel_targets_migration_adds_optional_targets() -> None:
    migration = next(item for item in available_migrations() if item.version == 47)
    assert migration.name == "automation_channel_targets"
    assert "poketwo_channel" in migration.sql
    assert "auto_reactions_channel" in migration.sql


def test_user_badges_migration_adds_owner_managed_badges() -> None:
    migration = next(item for item in available_migrations() if item.version == 48)
    assert migration.name == "user_badges"
    assert "CREATE TABLE IF NOT EXISTS user_badges" in migration.sql
    assert "user_id BIGINT NOT NULL" in migration.sql
    assert "created_at TIMESTAMP WITH TIME ZONE" in migration.sql
    assert "emoji_name TEXT NOT NULL" in migration.sql
    assert "emoji_id BIGINT" in migration.sql
    assert "is_custom BOOLEAN NOT NULL" in migration.sql
    assert "unicode BOOLEAN NOT NULL" in migration.sql
    assert "animated BOOLEAN NOT NULL" in migration.sql
    assert "badge_key TEXT NOT NULL" in migration.sql
    assert "text TEXT NOT NULL" in migration.sql
    assert "user_badges_user_key_idx" in migration.sql
    assert "ON CONFLICT (user_id, badge_key)" in migration.sql


def test_multiple_user_badges_migration_replaces_single_badge_keys() -> None:
    migration = next(item for item in available_migrations() if item.version == 53)
    assert migration.name == "multiple_user_badges"
    assert "badge_key = 'custom:' || id::text" in migration.sql
    assert "DROP INDEX IF EXISTS user_badges_user_key_idx" in migration.sql


def test_auto_reaction_channels_migration_preserves_existing_targets() -> None:
    migration = next(item for item in available_migrations() if item.version == 50)
    assert migration.name == "auto_reaction_channels"
    assert "CREATE TABLE IF NOT EXISTS guild_auto_reaction_channels" in migration.sql
    assert "guild_id BIGINT NOT NULL" in migration.sql
    assert "channel_id BIGINT NOT NULL" in migration.sql
    assert "SELECT guild_id, auto_reactions_channel" in migration.sql


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


def test_rock_paper_scissors_streak_migration_extends_shared_game_constraint() -> None:
    migration = next(item for item in available_migrations() if item.version == 58)
    assert migration.name == "rock_paper_scissors_streaks"
    assert "DROP CONSTRAINT IF EXISTS streak_game_stats_game_check" in migration.sql
    assert "rock_paper_scissors" in migration.sql


def test_streak_game_payout_migration_tracks_payout_leaderboards() -> None:
    migration = next(item for item in available_migrations() if item.version == 71)
    assert migration.name == "streak_game_payouts"
    assert "highest_streak_payout" in migration.sql
    assert "highest_payout" in migration.sql
    assert "highest_payout_streak" in migration.sql
    assert "streak_game_stats_payout_leaderboard_idx" in migration.sql


def test_lucky_roll_migration_tracks_player_results() -> None:
    migration = next(item for item in available_migrations() if item.version == 73)
    assert migration.name == "lucky_roll"
    assert "CREATE TABLE IF NOT EXISTS lucky_roll_stats" in migration.sql
    assert "coins_earned BIGINT" in migration.sql
    assert "coins_lost BIGINT" in migration.sql
    assert "lucky_roll_stats_leaderboard_idx" in migration.sql


def test_racing_emoji_migration_adds_user_ownership_and_equipped_index() -> None:
    migration = next(item for item in available_migrations() if item.version == 74)
    assert migration.name == "racing_emojis"
    assert "CREATE TABLE IF NOT EXISTS user_racing_emojis" in migration.sql
    assert "user_racing_emojis_one_equipped_idx" in migration.sql


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


def test_currency_colors_migration_seeds_presets_and_custom_ownership() -> None:
    migration = next(item for item in available_migrations() if item.version == 70)
    assert migration.name == "currency_colors"
    assert "CREATE TABLE IF NOT EXISTS color_catalog" in migration.sql
    assert "CREATE TABLE IF NOT EXISTS user_colors" in migration.sql
    assert "user_colors_one_equipped_idx" in migration.sql
    assert "('red', 'Red', '#FF0000', 5000)" in migration.sql
    assert "('white', 'White', '#FFFFFF', 10000)" in migration.sql
    assert "('black', 'Black', '#000000', 10000)" in migration.sql
    assert "('custom', 'Custom', '#000000', 25000)" in migration.sql
