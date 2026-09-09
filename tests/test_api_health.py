import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.types import Message, Scope

import api
from extensions.media_effects.processing import EffectResult
from web_api import accounts, history, media, webhooks
from web_api import state as api_state


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
        headers={"Content-Length": str(api_state.MAX_REQUEST_BYTES + 1)},
        content=b"{}",
    )
    assert response.status_code == 413


def test_rejects_oversized_chunked_request_without_content_length() -> None:
    response = request(
        "POST",
        "/oauth/exchange",
        content=b"x" * (api_state.MAX_REQUEST_BYTES + 1),
    )
    assert response.status_code == 413


def test_rejects_oversized_chunked_twitch_eventsub_payload() -> None:
    response = request(
        "POST",
        "/twitch/eventsub",
        content=b"x" * (api_state.MAX_WEBHOOK_BYTES + 1),
    )
    assert response.status_code == 413


def test_lastfm_state_is_session_bound_and_one_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(config={"keys": {"lastfm_secret": "state-secret"}})
    monkeypatch.setattr(api_state, "bot_ref", bot)

    state = accounts._lastfm_state(
        42,
        "website",
        session_id="session-a",
        browser_nonce="browser-a",
    )

    # A callback with the wrong browser credentials must not consume the state
    # and prevent the legitimate redirect from completing.
    with pytest.raises(HTTPException) as error:
        accounts._decode_lastfm_state(
            state,
            session_id="session-b",
            browser_nonce="browser-a",
        )
    assert error.value.status_code == 400

    assert accounts._decode_lastfm_state(
        state,
        session_id="session-a",
        browser_nonce="browser-a",
    ) == (42, "website", None, None)
    with pytest.raises(HTTPException) as error:
        accounts._decode_lastfm_state(
            state,
            session_id="session-a",
            browser_nonce="browser-a",
        )
    assert error.value.status_code == 400


@pytest.mark.parametrize(
    ("state_factory", "decoder", "prefix", "provider"),
    [
        ("_steam_state", "_decode_steam_state", "", "Steam"),
        ("_spotify_state", "_decode_spotify_state", "spotify_", "Spotify"),
        ("_anilist_state", "_decode_anilist_state", "anilist_", "AniList"),
    ],
)
def test_account_oauth_states_are_browser_bound_and_one_time(
    monkeypatch: pytest.MonkeyPatch,
    state_factory: str,
    decoder: str,
    prefix: str,
    provider: str,
) -> None:
    bot = SimpleNamespace(config={"keys": {"spotify_id": "client-id"}})
    monkeypatch.setattr(api_state, "bot_ref", bot)
    create_state = getattr(accounts, state_factory)
    decode_state = getattr(accounts, decoder)
    state = prefix + create_state(
        42,
        "website",
        session_id="session-a",
        browser_nonce="browser-a",
    )

    # A stolen callback state cannot be consumed from another browser.
    with pytest.raises(HTTPException) as error:
        decode_state(state, session_id="session-b", browser_nonce="browser-a")
    assert error.value.status_code == 400
    assert provider in str(error.value.detail)

    assert decode_state(state, session_id="session-a", browser_nonce="browser-a") == (
        42,
        "website",
        None,
        None,
    )
    with pytest.raises(HTTPException) as error:
        decode_state(state, session_id="session-a", browser_nonce="browser-a")
    assert error.value.status_code == 400


def test_twitch_eventsub_non_notification_message_ids_are_replay_protected() -> None:
    api_state._twitch_eventsub_replays.clear()
    assert asyncio.run(webhooks._claim_twitch_eventsub_message("event-1"))
    assert not asyncio.run(webhooks._claim_twitch_eventsub_message("event-1"))


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

    monkeypatch.setattr(api_state, "_check_pool", lambda: Pool())
    await history._history_visible_to(42, None, "session")
    with pytest.raises(HTTPException) as error:
        await history._history_visible_to(99, None, "session")
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_public_history_does_not_require_authentication(monkeypatch) -> None:
    class Pool:
        async def fetchval(self, sql: str, *_args):
            assert "history_public" in sql
            return True

    monkeypatch.setattr(api_state, "_check_pool", lambda: Pool())
    await history._history_visible_to(42, None, None)


def test_media_route_requires_key_and_returns_processed_file(monkeypatch) -> None:
    async def fake_render(data: bytes, effect: str, **options):
        assert data == b"image"
        assert effect == "pixelate"
        assert options == {"size": 12}
        return EffectResult(b"finished", "pixelate.png")

    monkeypatch.setattr(media, "render_image_effect", fake_render)
    monkeypatch.setattr(
        api_state,
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


def test_audio_effect_catalog_requires_key(monkeypatch) -> None:
    monkeypatch.setattr(
        api_state,
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
