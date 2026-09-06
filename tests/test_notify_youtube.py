from __future__ import annotations

import pytest

from extensions.events.youtube import YouTubeNotifications
from extensions.settings.notify import _youtube_event_label, youtube_list_details
from extensions.settings.notify_commands import Notify, _follow_display_name


def test_youtube_event_label_maps_command_choices() -> None:
    assert _youtube_event_label(["video", "live"]) == "uploads + live"
    assert _youtube_event_label(["video"]) == "uploads"
    assert _youtube_event_label(["live"]) == "live"
    assert _youtube_event_label(["short"]) == "shorts"
    assert _youtube_event_label(["community"]) == "community"
    assert _youtube_event_label(["video", "community"]) == "community, video"
    assert _youtube_event_label([]) == "no events"


def test_youtube_events_maps_option_to_event_types() -> None:
    assert Notify._youtube_events("uploads") == ("video",)
    assert Notify._youtube_events("live") == ("live",)
    assert Notify._youtube_events("both") == ("video", "live")
    assert Notify._youtube_events(None) == ("video", "live")
    assert Notify._youtube_events("UPLOADS") == ("video",)
    assert Notify._youtube_events("stream") == ("live",)


def test_follow_display_name_by_kind() -> None:
    row = {"channel_name": "Some Channel", "title": "Some Anime"}
    assert _follow_display_name(row, "youtube") == "Some Channel"
    assert _follow_display_name(row, "twitch") == "Some Channel"
    assert _follow_display_name(row, "anime") == "Some Anime"


def test_youtube_list_details_renders_dm_and_channel_destinations() -> None:
    rows = [
        {
            "id": 3,
            "channel_name": "Channel One",
            "channel_handle": "@one",
            "announce_channel_id": None,
            "event_types": ["video", "live"],
            "mention_role_id": None,
            "mention_everyone": False,
        },
        {
            "id": 4,
            "channel_name": "Channel Two",
            "channel_handle": None,
            "announce_channel_id": 123456789012345678,
            "event_types": ["video"],
            "mention_role_id": None,
            "mention_everyone": False,
        },
    ]
    text = youtube_list_details(rows)
    assert "3 · Channel One" in text
    assert "DM · @one · uploads + live" in text
    assert "4 · Channel Two" in text
    assert "<#123456789012345678> · uploads" in text


def test_youtube_owner_resolves_guild_or_user_scope() -> None:
    assert YouTubeNotifications._youtube_owner(
        {"guild_id": 1, "user_id": None}
    ) == ("guild_id", 1)
    assert YouTubeNotifications._youtube_owner(
        {"guild_id": None, "user_id": 42}
    ) == ("user_id", 42)


@pytest.mark.asyncio
async def test_youtube_destination_prefers_channel_then_dm_user() -> None:
    class Bot:
        def get_channel(self, channel_id: int):
            assert channel_id == 999
            return "the-channel"

        async def fetch_channel(self, _channel_id: int):
            raise RuntimeError("unused")

    notifications = YouTubeNotifications()
    notifications.bot = Bot()
    row = {"announce_channel_id": 999, "user_id": 42}
    assert await notifications._youtube_destination(row) == "the-channel"

    class BotDM:
        def get_user(self, user_id: int):
            assert user_id == 42
            return "the-user"

        async def fetch_user(self, _user_id: int):
            raise RuntimeError("unused")

    notifications.bot = BotDM()
    row_dm = {"announce_channel_id": None, "user_id": 42}
    assert await notifications._youtube_destination(row_dm) == "the-user"

    class BotMissing:
        def get_user(self, _user_id: int):
            return None

        async def fetch_user(self, user_id: int):
            assert user_id == 7
            return "fetched-user"

    notifications.bot = BotMissing()
    row_missing = {"announce_channel_id": None, "user_id": 7}
    assert await notifications._youtube_destination(row_missing) == "fetched-user"


def _make_notifications(executions: list[tuple]) -> YouTubeNotifications:
    class Pool:
        def __init__(self) -> None:
            self.calls = 0

        async def fetchval(self, *args):
            executions.append(args)
            self.calls += 1
            # The INSERT ... RETURNING claim returns the item_id on the first
            # call; later status/attempt lookups return nothing.
            return "video-id" if self.calls == 1 else None

        async def execute(self, *args):
            executions.append(args)

    class Notifications(YouTubeNotifications):
        def __init__(self) -> None:
            self.bot = type("Bot", (), {"pool": Pool()})()

        async def _announce_youtube(self, *_args) -> bool:
            return True

    return Notifications()


@pytest.mark.asyncio
async def test_announce_youtube_once_uses_scope_owner_for_claim() -> None:
    """DM follows must claim deliveries by user scope, not guild scope."""
    executions: list[tuple] = []
    notifications = _make_notifications(executions)
    row = {
        "guild_id": None,
        "user_id": 42,
        "youtube_channel_id": "UC1234567890123456789012",
    }
    claimed = await notifications._announce_youtube_once(
        row, "video-id", "video", {}
    )
    assert claimed is True
    # The first insert must be keyed on user_id so the partial unique index
    # (user_id, youtube_channel_id, item_id, event_type) is honored.
    insert_sql = str(executions[0][0])
    assert "user_id" in insert_sql
    assert (
        "ON CONFLICT (user_id, youtube_channel_id, item_id, event_type)"
        in insert_sql
    )


@pytest.mark.asyncio
async def test_announce_youtube_once_claims_by_guild_for_guild_rows() -> None:
    """Guild follows keep the existing guild-keyed delivery dedup."""
    executions: list[tuple] = []
    notifications = _make_notifications(executions)
    row = {
        "guild_id": 7,
        "user_id": None,
        "youtube_channel_id": "UC1234567890123456789012",
    }
    claimed = await notifications._announce_youtube_once(
        row, "video-id", "video", {}
    )
    assert claimed is True
    insert_sql = str(executions[0][0])
    assert "guild_id" in insert_sql
    assert (
        "ON CONFLICT (guild_id, youtube_channel_id, item_id, event_type)"
        in insert_sql
    )
