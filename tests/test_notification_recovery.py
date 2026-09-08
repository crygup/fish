from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest

from extensions.events.tasks import Tasks, TWITCH_EVENTSUB_CALLBACK
from utils.anilist import anilist_temporarily_unavailable


class Response:
    def __init__(self, status, data=None):
        self.status, self.data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.data


@pytest.mark.asyncio
async def test_twitch_recovery_removes_failed_subscription_and_adopts_enabled():
    def subscription(id, status, broadcaster="123"):
        return dict(
            id=id,
            status=status,
            type="stream.online",
            version="1",
            condition={"broadcaster_user_id": broadcaster},
            transport={"method": "webhook", "callback": TWITCH_EVENTSUB_CALLBACK},
        )

    active = subscription("active", "enabled")
    session = SimpleNamespace(
        get=Mock(
            return_value=Response(
                200,
                {
                    "data": [
                        subscription(
                            "unrelated", "notification_failures_exceeded", "456"
                        ),
                        subscription("dead", "notification_failures_exceeded"),
                        active,
                    ]
                },
            )
        ),
        delete=Mock(return_value=Response(204)),
    )
    cog = object.__new__(Tasks)
    cog.bot = SimpleNamespace(session=session, config={"keys": {"twitch_id": "test"}})
    assert await cog._find_twitch_eventsub_subscription("token", "123") == active
    assert session.delete.call_count == 1
    assert session.delete.call_args.kwargs["params"] == {"id": "dead"}

    session.get.return_value = Response(
        200, {"data": [subscription("dead", "authorization_revoked")]}
    )
    assert await cog._find_twitch_eventsub_subscription("token", "123") is None
    session.get.return_value = Response(
        200,
        {"data": [subscription("pending", "webhook_callback_verification_pending")]},
    )
    assert (await cog._find_twitch_eventsub_subscription("token", "123"))[
        "id"
    ] == "pending"
    assert session.delete.call_count == 2


def test_anilist_outages_are_distinct_from_missing_results_and_bad_credentials():
    assert anilist_temporarily_unavailable(
        503, {"errors": [{"message": "Unavailable"}]}
    )
    assert anilist_temporarily_unavailable(502, None)
    assert anilist_temporarily_unavailable(
        403, {"errors": [{"message": "API temporarily disabled"}]}
    )
    assert not anilist_temporarily_unavailable(
        403, {"errors": [{"message": "Forbidden"}]}
    )
    assert not anilist_temporarily_unavailable(
        404, {"errors": [{"message": "Not found"}]}
    )


@pytest.mark.asyncio
async def test_anilist_outage_stops_batches_and_backs_off():
    session = SimpleNamespace(post=Mock(return_value=Response(503, {})))
    cog = object.__new__(Tasks)
    cog.bot = SimpleNamespace(session=session, logger=Mock(), config={})
    assert await cog._anilist_media_batch(list(range(120))) == {}
    assert await cog._anilist_media_batch([1]) == {}
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_anilist_background_account_authentication_and_backoff():
    session = SimpleNamespace(
        post=Mock(
            return_value=Response(200, {"data": {"Page": {"media": [{"id": 1}]}}})
        )
    )
    pool = SimpleNamespace(fetchval=AsyncMock(return_value="dummy-token"))
    cog = object.__new__(Tasks)
    cog.bot = SimpleNamespace(
        session=session,
        pool=pool,
        logger=Mock(),
        config={"keys": {"anilist_background_user_id": 123}},
    )
    assert await cog._anilist_media_batch([1]) == {1: {"id": 1}}
    assert session.post.call_args.kwargs["headers"] == {
        "Authorization": "Bearer dummy-token"
    }
    assert pool.fetchval.call_args.args[1] == 123
    session.post.return_value = Response(401, {})
    assert await cog._anilist_media_batch([1]) == {}
    assert await cog._anilist_media_batch([1]) == {}
    assert session.post.call_count == 2
    cog._anilist_outage_retry_at = 0
    pool.fetchval.return_value = None
    assert await cog._anilist_media_batch([1]) == {}
    assert session.post.call_count == 2
