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
    ):
        assert table in sql
