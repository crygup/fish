from __future__ import annotations

from typing import Any

USER_ID_TABLES = (
    "accounts",
    "web_sessions",
    "user_settings",
    "user_statuses",
    "avatars",
    "username_logs",
    "display_name_logs",
    "discrim_logs",
    "nickname_logs",
    "opted_out",
    "message_xp",
    "command_logs",
    "highlights",
    "user_fishing",
    "caught_fish",
    "sold_fish",
    "fishing_rods",
    "fishing_bait",
    "fishing_catches",
    "fishing_accounts",
    "pokemon_solves",
    "mudae_dm_consent",
    "phone_consent",
)

GUILD_ID_TABLES = (
    "guild_icons",
    "guild_name_logs",
    "guild_avatars",
    "nickname_logs",
    "user_statuses",
    "command_logs",
    "guild_settings",
    "guild_log_channels",
    "guild_prefixes",
    "guild_opted_out",
    "member_join_logs",
    "guild_join_logs",
    "command_disables",
    "command_config",
    "plonks",
    "highlights",
    "twitch_follows",
    "twitch_announcement_deliveries",
    "pinboard_pins",
    "pokemon_solves",
    "mudae_timers",
    "mudae_subs",
    "mudae_channels",
    "honeypot_channels",
    "corn_reacts",
)


def _count(result: str) -> int:
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def erase_user(connection: Any, user_id: int) -> int:
    """Remove all rows that identify a Discord user in one transaction."""

    deleted = 0
    for table in USER_ID_TABLES:
        deleted += _count(
            await connection.execute(f"DELETE FROM {table} WHERE user_id = $1", user_id)
        )

    for table, column in (
        ("guild_avatars", "member_id"),
        ("member_join_logs", "member_id"),
        ("pokemon_guesses", "author_id"),
    ):
        deleted += _count(
            await connection.execute(
                f"DELETE FROM {table} WHERE {column} = $1", user_id
            )
        )

    deleted += _count(
        await connection.execute(
            "DELETE FROM reminders WHERE extra #>> '{args,0}' = $1", str(user_id)
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM user_rep WHERE user_id = $1", user_id
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM user_rep_logs WHERE user_id = $1 OR author_id = $1", user_id
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM pinboard_pins WHERE author_id = $1 OR target_id = $1", user_id
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM corn_reacts WHERE receiver_id = $1 OR giver_id = $1", user_id
        )
    )
    deleted += _count(
        await connection.execute(
            """WITH updated AS (
                   UPDATE mudae_subs
                   SET user_ids = array_remove(user_ids, $1)
                   WHERE $1 = ANY(user_ids)
                   RETURNING guild_id, item, user_ids
               )
               DELETE FROM mudae_subs target
               USING updated
               WHERE target.guild_id = updated.guild_id
                 AND target.item = updated.item
                 AND cardinality(updated.user_ids) = 0""",
            user_id,
        )
    )
    return deleted


async def erase_guild(connection: Any, guild_id: int) -> int:
    """Remove all database state owned by a Discord guild."""

    deleted = 0
    for table in GUILD_ID_TABLES:
        deleted += _count(
            await connection.execute(f"DELETE FROM {table} WHERE guild_id = $1", guild_id)
        )
    return deleted
