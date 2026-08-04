import json
from types import SimpleNamespace

import pytest

from extensions.events.youtube import (
    YouTubeNotifications,
    _extract_balanced_json,
    _iso_duration_seconds,
    normalize_youtube_events,
    parse_youtube_community_posts,
    youtube_websub_verify_token,
)


def test_normalize_youtube_events_filters_and_orders_values() -> None:
    assert normalize_youtube_events("COMMUNITY, video live unknown video") == (
        "video",
        "live",
        "community",
    )
    assert normalize_youtube_events([]) == ()


def test_iso_duration_seconds() -> None:
    assert _iso_duration_seconds("PT2M31S") == 151
    assert _iso_duration_seconds("P1DT2H3M4S") == 93784
    assert _iso_duration_seconds("not-a-duration") is None


def test_youtube_websub_verify_token_is_scoped_and_stable() -> None:
    channel_id = "UC1234567890123456789012"
    token = youtube_websub_verify_token("secret", channel_id)
    assert token == youtube_websub_verify_token("secret", channel_id)
    assert token != youtube_websub_verify_token("different", channel_id)
    assert token != youtube_websub_verify_token("secret", "UCabcdefghijklmnopqrstuv")


def test_extract_balanced_json_ignores_braces_inside_strings() -> None:
    document = 'before var ytInitialData = {"value": "} still text", "ok": true}; after'
    assert _extract_balanced_json(document, "var ytInitialData = ") == {
        "value": "} still text",
        "ok": True,
    }


def test_parse_youtube_community_posts() -> None:
    initial_data = {
        "contents": [
            {
                "backstagePostRenderer": {
                    "postId": "UgkxExample",
                    "authorText": {"simpleText": "Fishie"},
                    "contentText": {
                        "runs": [{"text": "A new "}, {"text": "community post"}]
                    },
                    "publishedTimeText": {"simpleText": "1 minute ago"},
                    "backstageAttachment": {
                        "backstageImageRenderer": {
                            "image": {
                                "thumbnails": [
                                    {"url": "https://example.com/small.jpg"},
                                    {"url": "https://example.com/large.jpg"},
                                ]
                            }
                        }
                    },
                }
            }
        ]
    }
    document = f"<script>var ytInitialData = {json.dumps(initial_data)};</script>"
    assert parse_youtube_community_posts(document) == [
        {
            "id": "UgkxExample",
            "author": "Fishie",
            "text": "A new community post",
            "published": "1 minute ago",
            "images": ["https://example.com/large.jpg"],
            "video_id": "",
        }
    ]


@pytest.mark.asyncio
async def test_dispatch_retries_when_any_destination_fails() -> None:
    class Pool:
        async def fetch(self, *_args):
            return [{"guild_id": 1}, {"guild_id": 2}]

    class Notifications(YouTubeNotifications):
        def __init__(self) -> None:
            self.bot = type("Bot", (), {"pool": Pool()})()

        async def _announce_youtube_once(
            self,
            row,
            _item_id: str,
            _event_type: str,
            _payload,
        ) -> bool:
            return row["guild_id"] == 1

    notifications = Notifications()
    with pytest.raises(RuntimeError, match="1 announcement destination"):
        await notifications._dispatch_youtube_item(
            "UC1234567890123456789012",
            "video-id",
            "video",
            {},
        )


@pytest.mark.asyncio
async def test_video_type_distinguishes_live_short_and_regular_uploads() -> None:
    class Response:
        status = 200
        url = SimpleNamespace(path="/shorts/video-id")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    notifications = YouTubeNotifications()
    notifications.bot = SimpleNamespace(session=Session())
    assert (
        await notifications._youtube_video_type(
            {
                "id": "video-id",
                "snippet": {"liveBroadcastContent": "live"},
                "contentDetails": {"duration": "PT30M"},
            }
        )
        == "live"
    )
    assert (
        await notifications._youtube_video_type(
            {
                "id": "video-id",
                "snippet": {},
                "contentDetails": {"duration": "PT45S"},
            }
        )
        == "short"
    )
    assert (
        await notifications._youtube_video_type(
            {
                "id": "video-id",
                "snippet": {},
                "contentDetails": {"duration": "PT20M"},
            }
        )
        == "video"
    )


@pytest.mark.asyncio
async def test_community_cursor_does_not_advance_after_delivery_failure() -> None:
    executions = []

    class Pool:
        async def fetch(self, *_args):
            return [{"youtube_channel_id": "UC1234567890123456789012"}]

        async def fetchval(self, *_args):
            return "old-post"

        async def execute(self, *args):
            executions.append(args)

    class Notifications(YouTubeNotifications):
        def __init__(self) -> None:
            self.bot = type("Bot", (), {"pool": Pool()})()

        async def _fetch_community_posts(self, _channel_id: str):
            return [{"id": "new-post"}, {"id": "old-post"}]

        async def _dispatch_youtube_item(self, *_args):
            raise RuntimeError("delivery failed")

    notifications = Notifications()
    with pytest.raises(RuntimeError, match="delivery failed"):
        await notifications.check_youtube_community_posts()
    assert executions == []
