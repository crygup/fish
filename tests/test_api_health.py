import asyncio
import inspect
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.types import Message, Scope

import api
from extensions.media_effects.processing import EffectResult


class ASGIResponse:
    def __init__(self, messages: list[Message]) -> None:
        start = next(item for item in messages if item["type"] == "http.response.start")
        self.status_code = start["status"]
        self.headers = {
            key.decode().lower(): value.decode() for key, value in start["headers"]
        }
        self.content = b"".join(
            item.get("body", b"")
            for item in messages
            if item["type"] == "http.response.body"
        )

    def json(self):
        return json.loads(self.content)


def request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> ASGIResponse:
    path_only, _, query = path.partition("?")
    request_sent = False
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": content,
                "more_body": False,
            }
        await asyncio.Future()
        raise RuntimeError("unreachable")

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path_only,
        "raw_path": path_only.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    asyncio.run(api.app(scope, receive, send))
    return ASGIResponse(messages)


def test_liveness_does_not_depend_on_external_services() -> None:
    response = request("GET", "/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_rejects_oversized_request_before_route_processing() -> None:
    response = request(
        "POST",
        "/oauth/exchange",
        headers={"Content-Length": str(api.MAX_REQUEST_BYTES + 1)},
        content=b"{}",
    )
    assert response.status_code == 413


def test_user_history_routes_accept_optional_session_for_private_self_access() -> None:
    for handler in (
        api.get_user_data,
        api.get_usernames,
        api.get_display_names,
        api.get_discrims,
        api.get_server_tags,
        api.get_status_history,
    ):
        parameters = inspect.signature(handler).parameters
        assert "authorization" in parameters
        assert "session_id" in parameters


def test_activity_is_available_as_an_individual_tracking_opt_out() -> None:
    assert "activity" in api.VALID_OPTOUTS
    assert "emoji" in api.VALID_OPTOUTS
    assert "downloads" in api.VALID_OPTOUTS


@pytest.mark.asyncio
async def test_private_history_only_allows_the_matching_session(
    monkeypatch,
) -> None:
    class Pool:
        async def fetchval(self, sql: str, *_args):
            if "history_public" in sql:
                return False
            if "web_sessions" in sql:
                return 42
            raise AssertionError(sql)

    monkeypatch.setattr(api, "_check_pool", lambda: Pool())
    await api._history_visible_to(42, None, "session")
    with pytest.raises(HTTPException) as error:
        await api._history_visible_to(99, None, "session")
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_public_history_does_not_require_authentication(monkeypatch) -> None:
    class Pool:
        async def fetchval(self, sql: str, *_args):
            assert "history_public" in sql
            return True

    monkeypatch.setattr(api, "_check_pool", lambda: Pool())
    await api._history_visible_to(42, None, None)


def test_youtube_websub_verification_requires_token_and_caps_lease(
    monkeypatch,
) -> None:
    channel_id = "UC1234567890123456789012"
    secret = "websub-secret"
    executions: list[tuple[str, tuple[object, ...]]] = []

    class Pool:
        async def fetchval(self, sql: str, *_args):
            assert "youtube_websub_subscriptions" in sql
            return 1

        async def execute(self, sql: str, *args):
            executions.append((sql, args))
            return "INSERT 0 1"

    monkeypatch.setattr(
        api,
        "bot_ref",
        SimpleNamespace(
            config={"keys": {"youtube_websub_secret": secret}},
            pool=Pool(),
        ),
    )
    query = {
        "hub.mode": "subscribe",
        "hub.topic": (
            "https://www.youtube.com/feeds/videos.xml" f"?channel_id={channel_id}"
        ),
        "hub.challenge": "challenge",
        "hub.lease_seconds": "999999999",
    }
    denied = request("GET", "/youtube/websub?" + urlencode(query))
    assert denied.status_code == 403

    query["hub.verify_token"] = api.youtube_websub_verify_token(
        secret,
        channel_id,
    )
    accepted = request("GET", "/youtube/websub?" + urlencode(query))
    assert accepted.status_code == 200
    assert accepted.content == b"challenge"
    assert executions[-1][1] == (channel_id, 864000)


def test_media_route_has_its_own_larger_body_limit() -> None:
    response = request(
        "POST",
        "/media/effects/invert",
        headers={"Content-Length": str(api.MAX_REQUEST_BYTES + 1)},
        content=b"x",
    )
    assert response.status_code != 413


def test_media_route_requires_key_and_returns_processed_file(monkeypatch) -> None:
    async def fake_render(data: bytes, effect: str, **options):
        assert data == b"image"
        assert effect == "pixelate"
        assert options == {"size": 12}
        return EffectResult(b"finished", "pixelate.png")

    monkeypatch.setattr(api, "render_image_effect", fake_render)
    monkeypatch.setattr(
        api,
        "bot_ref",
        SimpleNamespace(
            config={"keys": {"media_api": "test-key"}},
            media_semaphore=asyncio.Semaphore(1),
        ),
    )
    denied = request(
        "POST",
        "/media/effects/pixelate",
        headers={"Content-Type": "image/png"},
        content=b"image",
    )
    assert denied.status_code == 401

    response = request(
        "POST",
        '/media/effects/pixelate?options={"size":12}',
        headers={"Content-Type": "image/png", "X-API-Key": "test-key"},
        content=b"image",
    )
    assert response.status_code == 200
    assert response.content == b"finished"
    assert response.headers["content-type"] == "image/png"


def test_media_catalog_includes_new_image_effects() -> None:
    response = request("GET", "/media/effects")
    assert response.status_code == 200
    effects = set(response.json()["effects"])
    assert {
        "gifmagik",
        "gifswirl",
        "hallway",
        "huerotate",
        "magik",
        "meme",
        "parallax",
        "text",
        "combine",
        "overlay",
        "zoom",
    } <= effects


def test_audio_effect_catalog_requires_key(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "bot_ref",
        SimpleNamespace(config={"keys": {"media_api": "test-key"}}),
    )
    denied = request("GET", "/media/audio-effects")
    assert denied.status_code == 401

    response = request(
        "GET",
        "/media/audio-effects",
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200
    effects = response.json()["effects"]
    assert len(effects) >= 100
    assert effects[0]["id"] == 1
    assert all("path" not in effect for effect in effects)


def test_audio_overlay_requires_secondary_media(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "bot_ref",
        SimpleNamespace(
            config={"keys": {"media_api": "test-key"}},
            media_semaphore=asyncio.Semaphore(1),
        ),
    )
    response = request(
        "POST",
        "/media/effects/audiooverlay",
        headers={"Content-Type": "video/mp4", "X-API-Key": "test-key"},
        content=b"video",
    )
    assert response.status_code == 400
    assert "secondary_media_url" in response.json()["detail"]


def test_combine_api_fetches_and_uses_secondary_media(monkeypatch) -> None:
    async def fake_refresh(_bot, url: str) -> str:
        return url

    async def fake_fetch(_session, url: str, **_options):
        return SimpleNamespace(data=b"first" if url.endswith("/one") else b"second")

    async def fake_combine(first: bytes, second: bytes, **options):
        assert first == b"first"
        assert second == b"second"
        assert options == {
            "position": "left",
            "mode": "original",
            "audio": "none",
        }
        return EffectResult(b"combined", "combine.png")

    monkeypatch.setattr(api, "refresh_discord_attachment_url", fake_refresh)
    monkeypatch.setattr(api, "fetch_public_bytes", fake_fetch)
    monkeypatch.setattr(api, "render_combine_effect", fake_combine)
    monkeypatch.setattr(
        api,
        "bot_ref",
        SimpleNamespace(
            config={"keys": {"media_api": "test-key"}},
            media_semaphore=asyncio.Semaphore(1),
            session=object(),
        ),
    )
    response = request(
        "POST",
        "/media/effects/combine",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": "test-key",
        },
        content=json.dumps(
            {
                "media_url": "https://example.com/one",
                "secondary_media_url": "https://example.com/two",
                "options": {
                    "position": "left",
                    "mode": "original",
                    "audio": "none",
                },
            }
        ).encode(),
    )
    assert response.status_code == 200
    assert response.content == b"combined"


def test_overlay_api_fetches_and_uses_secondary_media(monkeypatch) -> None:
    async def fake_refresh(_bot, url: str) -> str:
        return url

    async def fake_fetch(_session, url: str, **_options):
        return SimpleNamespace(data=b"first" if url.endswith("/one") else b"second")

    async def fake_overlay(first: bytes, second: bytes, **options):
        assert first == b"first"
        assert second == b"second"
        assert options == {
            "opacity": 0.8,
            "size": "100x100",
            "overlay_audio": False,
        }
        return EffectResult(b"overlaid", "overlay-video.mp4")

    monkeypatch.setattr(api, "refresh_discord_attachment_url", fake_refresh)
    monkeypatch.setattr(api, "fetch_public_bytes", fake_fetch)
    monkeypatch.setattr(api, "render_overlay_effect", fake_overlay)
    monkeypatch.setattr(
        api,
        "bot_ref",
        SimpleNamespace(
            config={"keys": {"media_api": "test-key"}},
            media_semaphore=asyncio.Semaphore(1),
            session=object(),
        ),
    )
    response = request(
        "POST",
        "/media/effects/overlay",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": "test-key",
        },
        content=json.dumps(
            {
                "media_url": "https://example.com/one",
                "secondary_media_url": "https://example.com/two",
                "options": {
                    "opacity": 80,
                    "size": "100x100",
                    "overlay_audio": False,
                },
            }
        ).encode(),
    )
    assert response.status_code == 200
    assert response.content == b"overlaid"


def test_media_route_clamps_numeric_options_and_reports_it(monkeypatch) -> None:
    async def fake_render(data: bytes, effect: str, **options):
        assert data == b"image"
        assert effect == "pixelate"
        assert options == {"size": 128}
        return EffectResult(b"finished", "pixelate.png")

    monkeypatch.setattr(api, "render_image_effect", fake_render)
    monkeypatch.setattr(
        api,
        "bot_ref",
        SimpleNamespace(
            config={"keys": {"media_api": "test-key"}},
            media_semaphore=asyncio.Semaphore(1),
        ),
    )
    response = request(
        "POST",
        '/media/effects/pixelate?options={"size":1000}',
        headers={"Content-Type": "image/png", "X-API-Key": "test-key"},
        content=b"image",
    )

    assert response.status_code == 200
    assert "X-Fishie-Adjusted".lower() in response.headers
    assert "pixelate size" in response.headers["x-fishie-adjusted"]
