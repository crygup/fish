from core.privacy import erase_guild, erase_user


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *arguments: object) -> str:
        self.calls.append((sql, arguments))
        return "DELETE 1" if sql.startswith("DELETE") else "UPDATE 1"


async def test_full_user_erasure_covers_linked_and_legacy_data() -> None:
    connection = RecordingConnection()
    deleted = await erase_user(connection, 42)
    sql = "\n".join(item[0] for item in connection.calls)
    assert deleted > 20
    for table in (
        "accounts",
        "user_badges",
        "user_titles",
        "web_sessions",
        "reminders",
        "user_fishing",
        "caught_fish",
        "sold_fish",
        "fishing_accounts",
        "pokemon_solves",
        "corn_reacts",
        "reaction_logs",
        "reaction_tracking",
        "stag_logs",
        "user_statuses_legacy_backup",
        "user_status_history",
        "minigame_stats",
        "click_user_totals",
        "click_user_guild_totals",
        "tictactoe_games",
        "connectfour_games",
        "game_2048_stats",
        "lightsout_games",
        "wordle_games",
        "wordle_stats",
        "streak_game_stats",
        "wordbomb_stats",
        "video_aliases",
        "video_library_blocks",
        "video_library_hides",
        "post_uploads",
        "post_upload_blocks",
        "post_aliases",
        "post_library_blocks",
        "post_library_hides",
        "reputation_events",
        "reaction_logs",
        "guild_board_entries",
        "guild_board_blocks",
        "guild_boards SET emoji_set_by",
        "guild_protection SET configured_by",
        "guild_protection SET updated_by",
        "guild_protection_incidents",
        "guild_protection_locks",
        "guild_hourly_post_blocks",
        "user_racing_emojis",
        "currency_transactions",
        "currency_claims",
        "currency_daily_rewards",
        "currency_wagers",
        "currency_gambling_stats",
        "currency_wallets",
        "assigned_by",
        "locked_by",
    ):
        assert table in sql
    assert all(call[1] == (42,) or call[1] == ("42",) for call in connection.calls)


async def test_full_guild_erasure_covers_logs_configuration_and_deliveries() -> None:
    connection = RecordingConnection()
    await erase_guild(connection, 99)
    sql = "\n".join(item[0] for item in connection.calls)
    for table in (
        "guild_settings",
        "guild_log_channels",
        "command_logs",
        "nickname_logs",
        "twitch_follows",
        "twitch_announcement_deliveries",
        "youtube_follows",
        "youtube_announcement_deliveries",
        "honeypot_channels",
        "stag_logs",
        "user_statuses_legacy_backup",
        "user_status_history",
        "tictactoe_games",
        "connectfour_games",
        "lightsout_games",
        "post_uploads",
        "reputation_events",
        "guild_rep",
        "user_rep_logs",
        "guild_boards",
        "mudae_series",
        "guild_protection",
        "guild_protection_triggers",
        "guild_protection_incidents",
        "guild_protection_locks",
        "guild_hourly_posts",
        "guild_hourly_post_blocks",
    ):
        assert table in sql


async def test_erasure_covers_birthdays_rings_and_both_sides_of_relationships() -> None:
    connection = RecordingConnection()
    await erase_user(connection, 42)
    statements = [sql for sql, _ in connection.calls]
    for table in (
        "user_birthdays",
        "birthday_rewards",
        "user_racing_emojis",
        "user_rings",
        "social_user_settings",
        "social_marriage_cooldowns",
    ):
        assert f"DELETE FROM {table} WHERE user_id = $1" in statements
    assert statements.index("DELETE FROM user_racing_emojis WHERE user_id = $1") < statements.index(
        "DELETE FROM currency_wallets WHERE user_id = $1"
    )
    for table, predicate in (
        ("social_friend_requests", "requester_id = $1 OR recipient_id = $1"),
        ("social_friendships", "user_low_id = $1 OR user_high_id = $1"),
        ("social_follows", "follower_id = $1 OR followed_id = $1"),
        ("social_marriages", "proposer_id = $1 OR recipient_id = $1"),
        ("mudae_recent_claims", "claiming_user_id = $1"),
        (
            "guild_hourly_post_blocks",
            "user_id = $1 OR blocked_by = $1",
        ),
    ):
        assert f"DELETE FROM {table} WHERE {predicate}" in statements
    assert statements[-1] == "DELETE FROM currency_wallets WHERE user_id = $1"


async def test_guild_erasure_removes_recent_claims() -> None:
    connection = RecordingConnection()
    await erase_guild(connection, 42)
    assert (
        "DELETE FROM mudae_recent_claims WHERE guild_id = $1",
        (42,),
    ) in connection.calls
