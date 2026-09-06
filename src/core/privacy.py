from __future__ import annotations

from typing import Any

USER_ID_TABLES = (
    "accounts",
    "web_sessions",
    "user_settings",
    "user_statuses",
    "user_statuses_legacy_backup",
    "user_status_history",
    "avatars",
    "username_logs",
    "display_name_logs",
    "discrim_logs",
    "stag_logs",
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
    "mudae_wishes",
    "notify_twitch_follows",
    "notify_anime_follows",
    "youtube_follows",
    "phone_consent",
    "download_stats",
    "download_events",
    "minigame_stats",
    "click_user_totals",
    "click_user_guild_totals",
    "custom_role_assignments",
    "reaction_tracking",
    "global_user_blocks",
    "game_2048_stats",
    "game_2048_games",
    "lightsout_games",
    "wordle_games",
    "wordle_stats",
    "streak_game_stats",
    "wordbomb_stats",
    "lastletter_stats",
    "video_aliases",
    "video_library_blocks",
    "video_library_hides",
    "user_badges",
    "user_titles",
    "user_colors",
    "user_profiles",
    "guild_protection_locks",
    "currency_transactions",
    "currency_claims",
    "currency_daily_rewards",
    "currency_wagers",
    "currency_gambling_stats",
    "user_racing_emojis",  # Must precede the referenced currency wallet.
    "user_rings",  # Must precede the referenced currency wallet.
    "user_birthdays",
    "birthday_rewards",
    "social_user_settings",
    "social_marriage_cooldowns",
    "lottery_tickets",
)

GUILD_ID_TABLES = (
    "guild_icons",
    "guild_name_logs",
    "stag_logs",
    "guild_avatars",
    "nickname_logs",
    "user_statuses",
    "user_statuses_legacy_backup",
    "user_status_history",
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
    "notify_twitch_follows",
    "notify_anime_follows",
    "youtube_follows",
    "youtube_announcement_deliveries",
    "pinboard_pins",
    "pokemon_solves",
    "mudae_wishes",
    "mudae_recent_claims",
    "download_events",
    "mudae_timers",
    "mudae_subs",
    "mudae_channels",
    "mudae_series_bundles",
    "honeypot_channels",
    "corn_reacts",
    "tags",
    "emoji_stats",
    "custom_roles",
    "custom_role_assignments",
    "channel_locks",
    "tictactoe_games",
    "connectfour_games",
    "lightsout_games",
    "reaction_logs",
    "wordle_games",
    "user_rep_logs",
    "guild_boards",
    "guild_protection",
    "guild_protection_triggers",
    "guild_protection_incidents",
    "guild_protection_locks",
    "guild_hourly_posts",
    "guild_hourly_post_blocks",
)


def _count(result: str) -> int:
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def erase_user(connection: Any, user_id: int) -> int:
    """Remove all rows that identify a Discord user in one transaction."""

    deleted = 0
    # Deleting a marriage also removes both membership rows through its FK.
    # Remove either side of relationships, including rows owned by another user.
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
        deleted += _count(
            await connection.execute(f"DELETE FROM {table} WHERE {predicate}", user_id)
        )
    for table in USER_ID_TABLES:
        deleted += _count(
            await connection.execute(f"DELETE FROM {table} WHERE user_id = $1", user_id)
        )

    for table, column in (
        ("guild_avatars", "member_id"),
        ("member_join_logs", "member_id"),
        ("pokemon_guesses", "author_id"),
        ("tags", "author_id"),
        ("emoji_stats", "author_id"),
        ("custom_roles", "created_by"),
        ("custom_role_assignments", "assigned_by"),
        ("channel_locks", "locked_by"),
        ("global_user_blocks", "blocked_by"),
        ("global_command_disables", "disabled_by"),
        ("guild_protection_incidents", "actor_id"),
        ("lottery_rounds", "winner_user_id"),
    ):
        deleted += _count(
            await connection.execute(
                f"DELETE FROM {table} WHERE {column} = $1", user_id
            )
        )

    deleted += _count(
        await connection.execute(
            """DELETE FROM tictactoe_games
               WHERE player_x_id = $1
                  OR player_o_id = $1
                  OR winner_id = $1
                  OR loser_id = $1
                  OR started_by_id = $1""",
            user_id,
        )
    )

    deleted += _count(
        await connection.execute(
            """DELETE FROM connectfour_games
               WHERE player_yellow_id = $1
                  OR player_red_id = $1
                  OR winner_id = $1
                  OR loser_id = $1
                  OR started_by_id = $1""",
            user_id,
        )
    )

    deleted += _count(
        await connection.execute(
            "DELETE FROM reminders WHERE extra #>> '{args,0}' = $1", str(user_id)
        )
    )
    deleted += _count(
        await connection.execute("DELETE FROM user_rep WHERE user_id = $1", user_id)
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM user_rep_logs WHERE user_id = $1 OR author_id = $1", user_id
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM reputation_events WHERE giver_id = $1 OR receiver_id = $1",
            user_id,
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
            "DELETE FROM reaction_logs WHERE receiver_id = $1 OR giver_id = $1",
            user_id,
        )
    )
    # Board posts retain the source author so deleting a user must remove
    # those mirrored messages as well. Board block rows also identify both
    # the blocked user and the moderator who created the block.
    deleted += _count(
        await connection.execute(
            "DELETE FROM guild_board_entries WHERE source_author_id = $1",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            """DELETE FROM guild_board_blocks
               WHERE (target_type = 'user' AND target_id = $1)
                  OR blocked_by = $1""",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "UPDATE guild_boards SET emoji_set_by = NULL WHERE emoji_set_by = $1",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "UPDATE guild_protection SET configured_by = NULL WHERE configured_by = $1",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "UPDATE guild_protection SET updated_by = NULL WHERE updated_by = $1",
            user_id,
        )
    )
    # Video submissions can identify a user as the uploader, reviewer, or
    # person blocked from submitting.  Remove all rows that reference them so
    # account deletion does not leave an identifying audit trail behind.
    deleted += _count(
        await connection.execute(
            """DELETE FROM video_uploads
               WHERE uploader_id = $1 OR approved_by = $1 OR denied_by = $1""",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM video_upload_blocks WHERE user_id = $1 OR blocked_by = $1",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM video_library_blocks WHERE blocked_uploader_id = $1",
            user_id,
        )
    )
    # Post submissions and personal post-library preferences can identify the
    # uploader, reviewer, blocker, or alias owner. Remove all references on
    # account deletion, including rows owned by the deleted uploader.
    deleted += _count(
        await connection.execute(
            """DELETE FROM post_uploads
               WHERE uploader_id = $1 OR approved_by = $1 OR denied_by = $1""",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM post_upload_blocks WHERE user_id = $1 OR blocked_by = $1",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM post_aliases WHERE user_id = $1",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM post_library_blocks WHERE user_id = $1 OR blocked_uploader_id = $1",
            user_id,
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM post_library_hides WHERE user_id = $1",
            user_id,
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
    # Wallets are referenced by inventories, rewards, wagers, and lottery rows.
    # Delete the parent only after every dependent row has been handled.
    deleted += _count(
        await connection.execute(
            "DELETE FROM currency_wallets WHERE user_id = $1", user_id
        )
    )
    return deleted


async def erase_guild(connection: Any, guild_id: int) -> int:
    """Remove all database state owned by a Discord guild."""

    deleted = 0
    # User reputation is kept as a legacy aggregate, so remove the events for
    # this guild from each recipient's count before deleting the event rows.
    fetch = getattr(connection, "fetch", None)
    if fetch is not None:
        for row in await fetch(
            """
            SELECT receiver_id, COUNT(*) AS total
            FROM reputation_events
            WHERE guild_id = $1 AND receiver_id IS NOT NULL
            GROUP BY receiver_id
            """,
            guild_id,
        ):
            deleted += _count(
                await connection.execute(
                    """
                    UPDATE user_rep
                    SET count = GREATEST(count - $2, 0)
                    WHERE user_id = $1
                    """,
                    row["receiver_id"],
                    row["total"],
                )
            )

    # ``mudae_series`` is keyed by its bundle rather than directly by a
    # guild.  Remove the child rows explicitly before deleting their parent
    # bundles (the production schema also has an ON DELETE CASCADE, while the
    # explicit query keeps this correct for older installations).
    deleted += _count(
        await connection.execute(
            """DELETE FROM mudae_series
               WHERE bundle_id IN (
                   SELECT id FROM mudae_series_bundles WHERE guild_id = $1
               )""",
            guild_id,
        )
    )

    for table in GUILD_ID_TABLES:
        deleted += _count(
            await connection.execute(
                f"DELETE FROM {table} WHERE guild_id = $1", guild_id
            )
        )
    deleted += _count(
        await connection.execute(
            "DELETE FROM reputation_events WHERE guild_id = $1", guild_id
        )
    )
    deleted += _count(
        await connection.execute("DELETE FROM guild_rep WHERE guild_id = $1", guild_id)
    )
    # Video library rows use ``source_guild_id`` rather than the conventional
    # ``guild_id`` name because submissions may originate outside a guild.
    deleted += _count(
        await connection.execute(
            "DELETE FROM video_uploads WHERE source_guild_id = $1", guild_id
        )
    )
    deleted += _count(
        await connection.execute(
            "DELETE FROM post_uploads WHERE source_guild_id = $1", guild_id
        )
    )
    return deleted
