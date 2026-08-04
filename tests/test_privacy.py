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
        "web_sessions",
        "reminders",
        "user_fishing",
        "caught_fish",
        "sold_fish",
        "fishing_accounts",
        "pokemon_solves",
        "corn_reacts",
        "stag_logs",
        "user_statuses_legacy_backup",
        "user_status_history",
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
    ):
        assert table in sql
