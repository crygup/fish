"""
Fishie bot API | commands, stats, OAuth, and user data history.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import discord
from cachetools import TTLCache
from discord.ext import commands
from fastapi import (
    Body,
    Cookie,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

if TYPE_CHECKING:
    from core import Fishie

from core.privacy import erase_guild, erase_user
from extensions.events.youtube import (
    normalize_youtube_events,
    youtube_websub_verify_token,
)
from extensions.media_effects.audio_effects import (
    audio_effect_catalog as bundled_audio_effect_catalog,
)
from extensions.media_effects.audio_effects import (
    find_audio_effect,
)
from extensions.media_effects.commands import (
    PIPELINE_EFFECT_ALIASES,
    PIPELINE_EFFECTS,
    _normalize_effect_options,
    _renderer_effect_options,
    refresh_discord_attachment_url,
)
from extensions.media_effects.processing import (
    render_combine_effect,
    render_image_effect,
    render_overlay_effect,
    render_text_effect,
    render_video_effect,
)
from utils.credentials import decrypt_credential, encrypt_credential
from utils.network import (
    DISCORD_ATTACHMENT_HOSTS,
    fetch_public_bytes,
    validate_public_url,
)
from utils.rich_text import resolve_inline_images
from utils.vars import remove_user_badge

WEB_ORIGINS = frozenset({"https://crygup.com", "https://www.crygup.com"})
SESSION_COOKIE = "__Host-fishie_session"
OAUTH_STATE_COOKIE = "__Host-fishie_oauth_state"
LASTFM_STATE_COOKIE = "__Host-fishie_lastfm_state"
STEAM_STATE_COOKIE = "__Host-fishie_steam_state"
SPOTIFY_STATE_COOKIE = "__Host-fishie_spotify_state"
ANILIST_STATE_COOKIE = "__Host-fishie_anilist_state"
MESSAGE_CHALLENGE_COOKIE = "__Host-fishie_message_challenge"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
OAUTH_STATE_MAX_AGE = 10 * 60
MESSAGE_CHALLENGE_MAX_AGE = 10 * 60
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_MEDIA_API_BYTES = 50 * 1024 * 1024

app = FastAPI(title="Fishie API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(WEB_ORIGINS),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def protect_cookie_requests(request: Request, call_next):
    """Reject cross-origin state changes authenticated by the session cookie."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            request_limit = (
                MAX_MEDIA_API_BYTES
                if request.url.path.startswith("/media/effects/")
                else MAX_REQUEST_BYTES
            )
            if int(content_length) > request_limit:
                return JSONResponse(
                    status_code=413, content={"detail": "Request body is too large"}
                )
        except ValueError:
            return JSONResponse(
                status_code=400, content={"detail": "Invalid body size"}
            )
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.cookies.get(
        SESSION_COOKIE
    ):
        if request.headers.get("origin") not in WEB_ORIGINS:
            return JSONResponse(
                status_code=403, content={"detail": "Invalid request origin"}
            )
    return await call_next(request)


@app.get("/health/live", include_in_schema=False)
async def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    if bot_ref is None or bot_ref.is_closed() or not bot_ref.is_ready():
        raise HTTPException(503, "Bot is not ready")
    try:
        await asyncio.wait_for(_check_pool().fetchval("SELECT 1"), timeout=2)
    except Exception as error:
        raise HTTPException(503, "Database is not ready") from error
    return {"status": "ok"}


bot_ref: "Fishie | None" = None
MEDIA_API_EFFECTS = frozenset(
    {
        name
        for name, (engine, _, _) in PIPELINE_EFFECTS.items()
        if engine in {"image", "video"}
    }
    | {
        "meme",
        "text",
        "combine",
        "overlay",
        "audiooverlay",
        "audioreplace",
        "soundeffect",
    }
)
MEDIA_API_ALIASES = {
    **PIPELINE_EFFECT_ALIASES,
    "fade-in": "fadein",
    "fade-out": "fadeout",
    "slide-in": "slidein",
    "slide-out": "slideout",
}
TABLE_MAP = {
    "avatars": "avatars",
    "username_logs": "username_logs",
    "display_name_logs": "display_name_logs",
    "discrim_logs": "discrim_logs",
    "stag_logs": "stag_logs",
    "user_status_history": "user_status_history",
    "nickname_logs": "nickname_logs",
    "user_badges": "user_badges",
    "guild_icons": "guild_icons",
    "guild_name_logs": "guild_name_logs",
}
GUILD_TABLES = {"guild_icons", "guild_name_logs"}
LASTFM_CALLBACK_URL = "https://crygup.com/fishie"
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_STATE_TTL = 10 * 60
LASTFM_ACCOUNT_FIELDS = (
    "lastfm",
    "steam",
    "roblox",
    "letterboxd",
    "anilist",
)
STEAM_CALLBACK_URL = "https://crygup.com/fishie"
STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_STATE_TTL = 10 * 60
SPOTIFY_CALLBACK_URL = "https://crygup.com/fishie"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_STATE_TTL = 10 * 60
ANILIST_CALLBACK_URL = "https://crygup.com/fishie"
ANILIST_TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
ANILIST_STATE_TTL = 10 * 60
TWITCH_EVENTSUB_MAX_AGE = 10 * 60

# Last.fm states generated by older bot versions are signed but stateless.  A
# short-lived replay cache keeps those links one-time while new states use the
# server-side store below and can be bound to the browser session.
_legacy_lastfm_states_used = TTLCache[str, bool](maxsize=20_000, ttl=LASTFM_STATE_TTL)

# EventSub callback/revocation messages do not have a database event row to
# deduplicate (notifications do, via their message_id primary key). Keep a
# short-lived process-local claim cache so a signed replay cannot repeat a
# subscription status update. The EventSub signature timestamp is limited to
# the same ten-minute window.
_twitch_eventsub_replays = TTLCache[str, bool](
    maxsize=20_000, ttl=TWITCH_EVENTSUB_MAX_AGE
)
_twitch_eventsub_replay_lock = asyncio.Lock()


async def _claim_twitch_eventsub_message(message_id: str) -> bool:
    """Atomically claim a non-notification EventSub message.

    Keep the in-process lock for concurrent requests and persist a completed
    marker in the EventSub inbox for deployments with more than one API
    worker.  Callback/revocation payloads are intentionally marked ``done`` so
    the durable event worker never attempts to process their empty payload.
    """

    async with _twitch_eventsub_replay_lock:
        if message_id in _twitch_eventsub_replays:
            return False
        _twitch_eventsub_replays[message_id] = True
    if bot_ref is None or not getattr(bot_ref, "pool", None):
        return True
    try:
        result = await bot_ref.pool.execute(
            "INSERT INTO twitch_eventsub_events (message_id, payload, status) "
            "VALUES ($1, '{}'::jsonb, 'done') "
            "ON CONFLICT (message_id) DO NOTHING",
            message_id,
        )
    except Exception:
        _twitch_eventsub_replays.pop(message_id, None)
        raise
    if result == "INSERT 0 0":
        return False
    return True

# User resolution is occasionally needed for an arbitrary history lookup.  A
# bounded negative cache prevents repeated Discord API calls for invalid IDs or
# transiently unavailable users while keeping failures short-lived.
_discord_user_negative_cache = TTLCache[int, bool](maxsize=10_000, ttl=300)

# Public endpoints are intentionally available to the website, but all work is
# bounded per client and globally so attackers cannot turn them into unbounded
# Spotify/webhook proxies.  These are process-local limits; deployments with
# multiple workers should place a shared rate limiter at the edge as well.
SPOTIFY_COVER_RATE_LIMIT = 30
SPOTIFY_COVER_RATE_WINDOW = 60
_spotify_cover_rate = TTLCache[str, list[float]](maxsize=10_000, ttl=3600)

MESSAGE_GLOBAL_RATE_LIMIT = 60
MESSAGE_GLOBAL_RATE_WINDOW = 60
_message_global_requests: deque[float] = deque()
_message_rate_lock = asyncio.Lock()
_message_used_challenges = TTLCache[str, bool](
    maxsize=20_000, ttl=MESSAGE_CHALLENGE_MAX_AGE
)


async def _read_bounded_request(request: Request, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _require_media_api_key(supplied: str | None) -> None:
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    expected = str(bot_ref.config.get("keys", {}).get("media_api", "")).strip()
    if not expected:
        raise HTTPException(503, "Media API is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            401,
            "Invalid media API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def _media_api_options(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        if len(value) > 8_192:
            raise HTTPException(400, "Effect options are too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise HTTPException(400, "Effect options must be valid JSON") from error
    if not isinstance(value, dict) or len(value) > 20:
        raise HTTPException(400, "Effect options must be a JSON object")
    options: dict[str, Any] = {}
    for key, option in value.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key)
            or not isinstance(option, (str, int, float, bool))
            or isinstance(option, float)
            and not math.isfinite(option)
            or isinstance(option, str)
            and len(option) > 256
        ):
            raise HTTPException(400, "Effect options contain an invalid value")
        options[key] = option
    return options


@app.get("/media/effects")
async def media_effect_catalog():
    """List the effects accepted by the authenticated media endpoint."""
    return {
        "effects": sorted(MEDIA_API_EFFECTS),
        "max_bytes": MAX_MEDIA_API_BYTES,
        "authentication": "X-API-Key",
        "documentation": "docs/media-effects-api.md in the Fish repository",
    }


@app.get("/media/audio-effects")
async def media_audio_effect_catalog(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    """List the bundled audio clips accepted by the sound-effect processor."""
    _require_media_api_key(x_api_key)
    return {
        "effects": [
            {
                "id": effect.id,
                "name": effect.name,
                "display_name": effect.display_name,
                "category": effect.category,
                "duration": effect.duration,
            }
            for effect in bundled_audio_effect_catalog()
        ]
    }


@app.post("/media/effects/{effect}")
async def apply_media_effect_api(
    effect: str,
    request: Request,
    media_url: str | None = Query(None, max_length=2_048),
    secondary_media_url: str | None = Query(None, max_length=2_048),
    options: str = Query("{}"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    """Apply one media effect to a public URL or a raw uploaded file."""
    _require_media_api_key(x_api_key)
    normalized = effect.casefold().strip()
    normalized = MEDIA_API_ALIASES.get(normalized, normalized)
    if normalized not in MEDIA_API_EFFECTS:
        raise HTTPException(404, "Unknown media effect")
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")

    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    body = b""
    effect_options: dict[str, Any]
    if content_type == "application/json":
        raw_json = await _read_bounded_request(request, MAX_REQUEST_BYTES)
        try:
            payload = json.loads(raw_json or b"{}")
        except json.JSONDecodeError as error:
            raise HTTPException(400, "Request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        payload_url = payload.get("media_url")
        payload_secondary_url = payload.get("secondary_media_url")
        if payload_url is not None and not isinstance(payload_url, str):
            raise HTTPException(400, "media_url must be a string")
        if isinstance(payload_url, str) and len(payload_url) > 2_048:
            raise HTTPException(400, "media_url is too long")
        if payload_secondary_url is not None and not isinstance(
            payload_secondary_url, str
        ):
            raise HTTPException(400, "secondary_media_url must be a string")
        if (
            isinstance(payload_secondary_url, str)
            and len(payload_secondary_url) > 2_048
        ):
            raise HTTPException(400, "secondary_media_url is too long")
        media_url = media_url or payload_url
        secondary_media_url = secondary_media_url or payload_secondary_url
        effect_options = _media_api_options(payload.get("options", options))
    else:
        body = await _read_bounded_request(request, MAX_MEDIA_API_BYTES)
        effect_options = _media_api_options(options)

    if media_url and body:
        raise HTTPException(400, "Provide a media URL or an uploaded file, not both")
    if media_url:
        try:
            media_url = await refresh_discord_attachment_url(bot_ref, media_url)
            fetched = await fetch_public_bytes(
                bot_ref.session,
                media_url,
                max_bytes=MAX_MEDIA_API_BYTES,
                allowed_content_prefixes=("image/", "video/", "audio/"),
            )
        except commands.BadArgument as error:
            raise HTTPException(400, str(error)) from error
        body = fetched.data
    elif not body:
        raise HTTPException(
            400,
            "Provide media_url in JSON/query parameters or upload raw media bytes",
        )
    elif content_type and not (
        content_type.startswith(("image/", "video/", "audio/"))
        or content_type == "application/octet-stream"
    ):
        raise HTTPException(415, "Upload an image, GIF, video, or audio file")

    secondary_data: bytes | None = None
    if normalized == "soundeffect":
        try:
            selected = find_audio_effect(str(effect_options.pop("effect", "random")))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        secondary_data = await asyncio.to_thread(selected.path.read_bytes)
    elif secondary_media_url:
        try:
            secondary_media_url = await refresh_discord_attachment_url(
                bot_ref,
                secondary_media_url,
            )
            secondary = await fetch_public_bytes(
                bot_ref.session,
                secondary_media_url,
                max_bytes=MAX_MEDIA_API_BYTES,
                allowed_content_prefixes=("image/", "video/", "audio/"),
            )
        except commands.BadArgument as error:
            raise HTTPException(400, str(error)) from error
        secondary_data = secondary.data

    if (
        normalized in {"overlay", "audiooverlay", "audioreplace", "combine"}
        and secondary_data is None
    ):
        raise HTTPException(
            400,
            "This effect requires secondary_media_url.",
        )

    effect_options, adjustments = _normalize_effect_options(
        normalized,
        effect_options,
    )
    renderer_options = _renderer_effect_options(normalized, effect_options)
    try:
        async with bot_ref.media_semaphore:
            async with asyncio.timeout(60):
                engine = PIPELINE_EFFECTS[normalized][0]
                if normalized == "text":
                    text = str(renderer_options.get("text", ""))
                    renderer_options["inline_images"] = await resolve_inline_images(
                        bot_ref.session, [text]
                    )
                    result = await render_text_effect(body, **renderer_options)
                elif normalized == "combine":
                    if secondary_data is None:
                        raise ValueError("Combine requires secondary media.")
                    result = await render_combine_effect(
                        body,
                        secondary_data,
                        **renderer_options,
                    )
                elif normalized == "overlay":
                    if secondary_data is None:
                        raise ValueError("Overlay requires secondary media.")
                    result = await render_overlay_effect(
                        body,
                        secondary_data,
                        **renderer_options,
                    )
                elif engine == "video" or normalized in {
                    "audiooverlay",
                    "audioreplace",
                    "soundeffect",
                }:
                    result = await render_video_effect(
                        body,
                        normalized,
                        second_data=secondary_data,
                        **renderer_options,
                    )
                else:
                    result = await render_image_effect(
                        body,
                        normalized,
                        **renderer_options,
                    )
    except TimeoutError as error:
        raise HTTPException(408, "The effect took longer than 60 seconds") from error
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error

    if len(result.data) > MAX_MEDIA_API_BYTES:
        raise HTTPException(413, "Generated media is too large")
    filename = result.filename.replace("/", "_").replace("\\", "_")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }
    if adjustments:
        headers["X-Fishie-Adjusted"] = json.dumps(adjustments, ensure_ascii=True)[:2048]
    return Response(
        result.data,
        media_type=media_type,
        headers=headers,
    )


def _lastfm_state(
    user_id: int,
    source: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    """Create a one-time Last.fm state kept in the bot process.

    Last.fm redirects through a public callback, so a signed, stateless value
    is not enough: somebody who obtains the value could race the legitimate
    browser and attach their Last.fm account to the target Discord user.  New
    states are opaque, removed on first decode, and (for website flows) bound
    to both the Fishie session and a short-lived browser nonce.
    """
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(bot_ref, "_lastfm_oauth_states", None)
    if states is None:
        states = bot_ref._lastfm_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)

    token = secrets.token_urlsafe(32)
    state_data: dict[str, Any] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + LASTFM_STATE_TTL,
    }
    if session_id:
        state_data["session_hash"] = _session_hash(session_id)
    if browser_nonce:
        state_data["browser_nonce_hash"] = _session_hash(browser_nonce)
    states[token] = state_data
    return f"lastfm_{token}"


def _lastfm_authorization_url(
    user_id: int,
    source: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    state = _lastfm_state(
        user_id,
        source,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    callback = f"{LASTFM_CALLBACK_URL}?{urlencode({'lastfm_state': state})}"
    return "https://www.last.fm/api/auth/?" + urlencode(
        {"api_key": bot_ref.config["keys"]["lastfm_cb"], "cb": callback}
    )


def _steam_state(
    user_id: int,
    source: str,
    channel_id: int | None = None,
    message_id: int | None = None,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(bot_ref, "_steam_oauth_states", None)
    if states is None:
        states = bot_ref._steam_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    states[token] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + STEAM_STATE_TTL,
    }
    if session_id:
        states[token]["session_hash"] = _session_hash(session_id)
    if browser_nonce:
        states[token]["browser_nonce_hash"] = _session_hash(browser_nonce)
    if channel_id is not None and message_id is not None:
        states[token]["channel_id"] = int(channel_id)
        states[token]["message_id"] = int(message_id)
    return token


def _steam_authorization_url(
    user_id: int,
    source: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    state = _steam_state(
        user_id,
        source,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    callback = f"{STEAM_CALLBACK_URL}?{urlencode({'steam_state': state})}"
    return (
        STEAM_OPENID_URL
        + "?"
        + urlencode(
            {
                "openid.ns": "http://specs.openid.net/auth/2.0",
                "openid.mode": "checkid_setup",
                "openid.return_to": callback,
                "openid.realm": "https://crygup.com/",
                "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
                "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            }
        )
    )


def _decode_steam_state(
    state: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> tuple[int, str, int | None, int | None]:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    states = getattr(bot_ref, "_steam_oauth_states", None) or {}
    # Keep the state available until all browser/session checks pass. A
    # callback with a stolen state must not invalidate the legitimate redirect.
    payload = states.get(state)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Steam connection state")
    try:
        user_id = int(payload["user_id"])
        source = payload["source"]
        expires = int(payload["expires"])
        expected_session = payload.get("session_hash")
        expected_nonce = payload.get("browser_nonce_hash")
        channel_id = int(payload["channel_id"]) if payload.get("channel_id") else None
        message_id = int(payload["message_id"]) if payload.get("message_id") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Invalid Steam connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid Steam connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The Steam connection link has expired")
    if source == "website":
        if not expected_session or not session_id or not hmac.compare_digest(
            str(expected_session), _session_hash(session_id)
        ):
            raise HTTPException(400, "Invalid Steam browser session")
        if not expected_nonce or not browser_nonce or not hmac.compare_digest(
            str(expected_nonce), _session_hash(browser_nonce)
        ):
            raise HTTPException(400, "Invalid Steam browser state")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
    states.pop(state, None)
    return user_id, source, channel_id, message_id


def _spotify_state(
    user_id: int,
    source: str,
    channel_id: int | None = None,
    message_id: int | None = None,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(bot_ref, "_spotify_oauth_states", None)
    if states is None:
        states = bot_ref._spotify_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    states[token] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + SPOTIFY_STATE_TTL,
    }
    if session_id:
        states[token]["session_hash"] = _session_hash(session_id)
    if browser_nonce:
        states[token]["browser_nonce_hash"] = _session_hash(browser_nonce)
    if channel_id is not None and message_id is not None:
        states[token]["channel_id"] = int(channel_id)
        states[token]["message_id"] = int(message_id)
    return token


def _spotify_authorization_url(
    user_id: int,
    source: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    state = _spotify_state(
        user_id,
        source,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    return "https://accounts.spotify.com/authorize?" + urlencode(
        {
            "client_id": bot_ref.config["keys"]["spotify_id"] if bot_ref else "",
            "response_type": "code",
            "redirect_uri": SPOTIFY_CALLBACK_URL,
            "scope": "user-read-private",
            "state": f"spotify_{state}",
        }
    )


def _decode_spotify_state(
    state: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> tuple[int, str, int | None, int | None]:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    if not state.startswith("spotify_"):
        raise HTTPException(400, "Invalid Spotify connection state")
    states = getattr(bot_ref, "_spotify_oauth_states", None) or {}
    state_key = state.removeprefix("spotify_")
    payload = states.get(state_key)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Spotify connection state")
    try:
        user_id = int(payload["user_id"])
        source = payload["source"]
        expires = int(payload["expires"])
        expected_session = payload.get("session_hash")
        expected_nonce = payload.get("browser_nonce_hash")
        channel_id = int(payload["channel_id"]) if payload.get("channel_id") else None
        message_id = int(payload["message_id"]) if payload.get("message_id") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Invalid Spotify connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid Spotify connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The Spotify connection link has expired")
    if source == "website":
        if not expected_session or not session_id or not hmac.compare_digest(
            str(expected_session), _session_hash(session_id)
        ):
            raise HTTPException(400, "Invalid Spotify browser session")
        if not expected_nonce or not browser_nonce or not hmac.compare_digest(
            str(expected_nonce), _session_hash(browser_nonce)
        ):
            raise HTTPException(400, "Invalid Spotify browser state")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
    states.pop(state_key, None)
    return user_id, source, channel_id, message_id


def _anilist_state(
    user_id: int,
    source: str,
    channel_id: int | None = None,
    message_id: int | None = None,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(bot_ref, "_anilist_oauth_states", None)
    if states is None:
        states = bot_ref._anilist_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    states[token] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + ANILIST_STATE_TTL,
    }
    if session_id:
        states[token]["session_hash"] = _session_hash(session_id)
    if browser_nonce:
        states[token]["browser_nonce_hash"] = _session_hash(browser_nonce)
    if channel_id is not None and message_id is not None:
        states[token]["channel_id"] = int(channel_id)
        states[token]["message_id"] = int(message_id)
    return token


def _anilist_authorization_url(
    user_id: int,
    source: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    state = _anilist_state(
        user_id,
        source,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    return "https://anilist.co/api/v2/oauth/authorize?" + urlencode(
        {
            "client_id": bot_ref.config["keys"]["anilist_id"],
            "redirect_uri": ANILIST_CALLBACK_URL,
            "response_type": "code",
            "state": f"anilist_{state}",
        }
    )


def _decode_anilist_state(
    state: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> tuple[int, str, int | None, int | None]:
    if not bot_ref or not state.startswith("anilist_"):
        raise HTTPException(400, "Invalid AniList connection state")
    states = getattr(bot_ref, "_anilist_oauth_states", None) or {}
    state_key = state.removeprefix("anilist_")
    payload = states.get(state_key)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid AniList connection state")
    try:
        user_id = int(payload["user_id"])
        source = payload["source"]
        expires = int(payload["expires"])
        expected_session = payload.get("session_hash")
        expected_nonce = payload.get("browser_nonce_hash")
        channel_id = int(payload["channel_id"]) if payload.get("channel_id") else None
        message_id = int(payload["message_id"]) if payload.get("message_id") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Invalid AniList connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid AniList connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The AniList connection link has expired")
    if source == "website":
        if not expected_session or not session_id or not hmac.compare_digest(
            str(expected_session), _session_hash(session_id)
        ):
            raise HTTPException(400, "Invalid AniList browser session")
        if not expected_nonce or not browser_nonce or not hmac.compare_digest(
            str(expected_nonce), _session_hash(browser_nonce)
        ):
            raise HTTPException(400, "Invalid AniList browser state")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
    states.pop(state_key, None)
    return user_id, source, channel_id, message_id


def _decode_lastfm_state(
    state: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> tuple[int, str, int | None, int | None]:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")

    # States issued by current versions are opaque and removed before any
    # network request is made, making callback handling one-time even when two
    # requests arrive concurrently.
    if state.startswith("lastfm_"):
        states = getattr(bot_ref, "_lastfm_oauth_states", None) or {}
        state_key = state.removeprefix("lastfm_")
        # Keep the state available until all browser/session checks pass. A
        # callback with a stolen state but the wrong cookie must not be able to
        # invalidate the legitimate redirect.
        payload = states.get(state_key)
        if not isinstance(payload, dict):
            raise HTTPException(400, "Invalid Last.fm connection state")
        try:
            user_id = int(payload["user_id"])
            source = payload["source"]
            expires = int(payload["expires"])
            expected_session = payload.get("session_hash")
            expected_nonce = payload.get("browser_nonce_hash")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Invalid Last.fm connection state")
        if source not in {"discord", "website"}:
            raise HTTPException(400, "Invalid Last.fm connection source")
        if expires < int(time.time()):
            raise HTTPException(400, "The Last.fm connection link has expired")
        if expected_session:
            if not session_id or not hmac.compare_digest(
                str(expected_session), _session_hash(session_id)
            ):
                raise HTTPException(400, "Invalid Last.fm browser session")
        if expected_nonce:
            if not browser_nonce or not hmac.compare_digest(
                str(expected_nonce), _session_hash(browser_nonce)
            ):
                raise HTTPException(400, "Invalid Last.fm browser state")
        states.pop(state_key, None)
        return (
            user_id,
            source,
            int(payload["channel_id"]) if payload.get("channel_id") else None,
            int(payload["message_id"]) if payload.get("message_id") else None,
        )

    # Accept a short-lived state generated by an older bot during deployment,
    # but make it one-time so it cannot be replayed indefinitely.
    legacy_key = hashlib.sha256(state.encode("utf-8")).hexdigest()
    if legacy_key in _legacy_lastfm_states_used:
        raise HTTPException(400, "Invalid or already used Last.fm connection state")
    try:
        encoded, supplied_signature = state.split(".", 1)
        expected_signature = hmac.new(
            bot_ref.config["keys"]["lastfm_secret"].encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid signature")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        user_id = int(payload["user_id"])
        source = payload["source"]
        expires = int(payload["expires"])
        channel_id = int(payload["channel_id"]) if payload.get("channel_id") else None
        message_id = int(payload["message_id"]) if payload.get("message_id") else None
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise HTTPException(400, "Invalid Last.fm connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid Last.fm connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The Last.fm connection link has expired")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
    _legacy_lastfm_states_used[legacy_key] = True
    return user_id, source, channel_id, message_id


def _lastfm_api_signature(api_key: str, token: str, secret: str) -> str:
    signature = f"api_key{api_key}methodauth.getSessiontoken{token}{secret}"
    return hashlib.md5(signature.encode()).hexdigest()


async def _steam_display_name(steam_id: str) -> str | None:
    if not bot_ref:
        return None
    api_key = bot_ref.config["keys"].get("steam")
    if not api_key:
        return None
    try:
        async with bot_ref.session.get(
            "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
            params={"key": api_key, "steamids": steam_id, "format": "json"},
        ) as response:
            profile = await response.json(content_type=None)
        response_data = profile.get("response") if isinstance(profile, dict) else None
        players = (
            response_data.get("players", []) if isinstance(response_data, dict) else []
        )
        if isinstance(players, list) and players and isinstance(players[0], dict):
            name = players[0].get("personaname")
            return str(name) if name else None
    except (aiohttp.ClientError, ValueError, TypeError):
        pass
    return None


async def _refresh_discord_accounts_message(
    user_id: int, channel_id: int | None, message_id: int | None
) -> None:
    if not bot_ref or channel_id is None or message_id is None:
        return
    try:
        from extensions.settings import ManageAccountsView

        channel: Any = bot_ref.get_channel(channel_id)
        if channel is None:
            channel = await bot_ref.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        row = await bot_ref.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, anilist FROM accounts "
            "WHERE user_id = $1",
            user_id,
        )
        author = bot_ref.get_user(user_id) or SimpleNamespace(id=user_id)
        ctx = SimpleNamespace(bot=bot_ref, author=author)
        await message.edit(
            view=ManageAccountsView(
                cast(Any, ctx),
                row=row,
                lastfm_connected=bool(row and row["lastfm"]),
                steam_connected=bool(row and row["steam"]),
                anilist_connected=bool(row and row["anilist"]),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as error:
        bot_ref.logger.warning(
            "Could not refresh Discord accounts message after account OAuth: %s",
            error,
        )


def _schedule_discord_accounts_refresh(
    user_id: int, channel_id: int | None, message_id: int | None
) -> None:
    if not bot_ref or channel_id is None or message_id is None:
        return
    tasks = bot_ref._oauth_refresh_tasks
    task = asyncio.create_task(
        _refresh_discord_accounts_message(user_id, channel_id, message_id)
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def init(bot: "Fishie") -> None:
    global bot_ref
    bot_ref = bot


def _active_application_id() -> int:
    """Return the application ID belonging to the running bot instance.

    During normal operation :class:`core.bot.Fishie` exposes an
    ``active_application_id`` property.  Keep a configuration fallback for
    lightweight API tests and older bot objects that predate dual-instance
    support.
    """

    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    active_id = getattr(bot_ref, "active_application_id", None)
    if active_id is None:
        active_id = getattr(bot_ref, "active_bot_id", None)
    if active_id is None:
        active_id = bot_ref.config.get("ids", {}).get("bot_id")
    try:
        return int(active_id)
    except (TypeError, ValueError) as error:
        raise HTTPException(503, "Bot application ID is not configured") from error


def _active_client_secret() -> str:
    """Return the OAuth/challenge secret for the running bot instance."""

    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    active_secret = getattr(bot_ref, "oauth_client_secret", None)
    if active_secret:
        return str(active_secret)
    keys = bot_ref.config.get("keys", {})
    secret = keys.get("client_secret")
    if not isinstance(secret, str) or not secret:
        raise HTTPException(503, "Bot client secret is not configured")
    return secret


def _check_pool():
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    pool = bot_ref.pool
    if not pool:
        raise HTTPException(503, "Database not connected")
    return pool


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


async def _create_web_session(
    user_id: int, discord_access_token: str, expires_in: int | None
) -> tuple[str, int]:
    """Create an opaque browser session and keep the Discord token server-side."""
    pool = _check_pool()
    lifetime = max(60, min(int(expires_in or SESSION_MAX_AGE), SESSION_MAX_AGE))
    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=lifetime)
    await pool.execute("DELETE FROM web_sessions WHERE expires_at <= now()")
    await pool.execute(
        """INSERT INTO web_sessions
           (session_id_hash, user_id, discord_access_token, expires_at)
           VALUES ($1, $2, $3, $4)""",
        _session_hash(session_id),
        user_id,
        encrypt_credential(discord_access_token),
        expires_at,
    )
    return session_id, lifetime


async def _session_access_token(session_id: str) -> str:
    pool = _check_pool()
    row = await pool.fetchrow(
        "SELECT discord_access_token, expires_at FROM web_sessions "
        "WHERE session_id_hash = $1",
        _session_hash(session_id),
    )
    if not row:
        raise HTTPException(401, "Invalid or expired session")
    expires_at = row["expires_at"]
    if expires_at <= datetime.now(timezone.utc):
        await pool.execute(
            "DELETE FROM web_sessions WHERE session_id_hash = $1",
            _session_hash(session_id),
        )
        raise HTTPException(401, "Session expired")
    await pool.execute(
        "UPDATE web_sessions SET last_seen_at = now() WHERE session_id_hash = $1",
        _session_hash(session_id),
    )
    token = decrypt_credential(row["discord_access_token"])
    if token is None:
        raise HTTPException(401, "Session credential is unavailable")
    return token


def _verify_twitch_eventsub(request: Request, body: bytes) -> None:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    keys = bot_ref.config["keys"]
    secret = keys.get("twitch_eventsub_secret")
    if not secret:
        raise HTTPException(503, "Twitch EventSub is not configured")

    message_id = request.headers.get("Twitch-Eventsub-Message-Id")
    timestamp = request.headers.get("Twitch-Eventsub-Message-Timestamp")
    supplied_signature = request.headers.get("Twitch-Eventsub-Message-Signature")
    if not message_id or not timestamp or not supplied_signature:
        raise HTTPException(403, "Missing Twitch EventSub signature headers")

    try:
        message_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(403, "Invalid Twitch EventSub timestamp")
    if message_time.tzinfo is None:
        message_time = message_time.replace(tzinfo=timezone.utc)
    if abs(time.time() - message_time.timestamp()) > TWITCH_EVENTSUB_MAX_AGE:
        raise HTTPException(403, "Expired Twitch EventSub message")

    expected_signature = (
        "sha256="
        + hmac.new(
            secret.encode(),
            message_id.encode() + timestamp.encode() + body,
            hashlib.sha256,
        ).hexdigest()
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(403, "Invalid Twitch EventSub signature")


@app.post("/twitch/eventsub")
async def twitch_eventsub(request: Request):
    """Receive verified Twitch EventSub stream notifications."""
    body = await request.body()
    _verify_twitch_eventsub(request, body)
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    bot = bot_ref

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid Twitch EventSub payload")

    message_type = request.headers.get("Twitch-Eventsub-Message-Type")
    message_id = request.headers.get("Twitch-Eventsub-Message-Id")
    bot.logger.info(
        "Received Twitch EventSub message type=%s", message_type or "unknown"
    )
    # Verification and revocation messages update subscription state directly
    # and therefore do not get the notification table's primary-key dedupe.
    # Claim their signed message ID before performing those updates so a replay
    # cannot repeat the side effect during Twitch's ten-minute validity window.
    if message_type in {"webhook_callback_verification", "revocation"}:
        if not message_id or not await _claim_twitch_eventsub_message(message_id):
            return {"ok": True, "duplicate": True}
    if message_type == "webhook_callback_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(400, "Missing Twitch EventSub challenge")
        subscription = payload.get("subscription")
        if isinstance(subscription, dict) and subscription.get("id"):
            await bot.pool.execute(
                "UPDATE twitch_eventsub_subscriptions "
                "SET status = 'enabled', updated_at = now() "
                "WHERE subscription_id = $1",
                subscription["id"],
            )
        return PlainTextResponse(challenge)

    subscription = payload.get("subscription")
    if not isinstance(subscription, dict):
        raise HTTPException(400, "Missing Twitch EventSub subscription")

    if message_type == "revocation":
        if bot_ref:
            await bot_ref.pool.execute(
                "UPDATE twitch_eventsub_subscriptions "
                "SET status = $2, updated_at = now() WHERE subscription_id = $1",
                subscription.get("id"),
                subscription.get("status", "revoked"),
            )
        return {"ok": True}

    if message_type != "notification":
        raise HTTPException(400, "Unsupported Twitch EventSub message type")

    event = payload.get("event")
    if not isinstance(event, dict) or not message_id:
        raise HTTPException(400, "Missing Twitch EventSub event")
    event = dict(event)
    event["type"] = subscription.get("type")

    events_cog: Any = bot_ref.get_cog("Events")
    if events_cog is None or not hasattr(events_cog, "handle_twitch_event"):
        raise HTTPException(503, "Twitch event handler is not ready")

    pool = _check_pool()
    result = await pool.execute(
        "INSERT INTO twitch_eventsub_events (message_id, payload) VALUES ($1, $2::jsonb) "
        "ON CONFLICT (message_id) DO NOTHING",
        message_id,
        event,
    )
    if result == "INSERT 0 0":
        return {"ok": True, "duplicate": True}

    task = asyncio.create_task(events_cog.process_twitch_event_message(message_id))
    tasks = bot._eventsub_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"ok": True}


def _youtube_channel_from_topic(topic: str) -> str | None:
    parsed = urlsplit(topic)
    if parsed.scheme != "https" or parsed.hostname not in {
        "www.youtube.com",
        "youtube.com",
    }:
        return None
    if parsed.path != "/feeds/videos.xml":
        return None
    values = parse_qs(parsed.query).get("channel_id", [])
    channel_id = values[0] if len(values) == 1 else ""
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        return None
    return channel_id


def _verify_youtube_websub_signature(body: bytes, supplied: str | None) -> None:
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    keys = bot_ref.config.get("keys", {})
    secret = keys.get("youtube_websub_secret")
    if not secret:
        raise HTTPException(503, "YouTube WebSub is not configured")
    if not supplied or "=" not in supplied:
        raise HTTPException(403, "Missing YouTube WebSub signature")
    algorithm, signature = supplied.split("=", 1)
    digest = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}.get(algorithm.lower())
    if digest is None:
        raise HTTPException(403, "Unsupported YouTube WebSub signature")
    expected = hmac.new(str(secret).encode(), body, digest).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise HTTPException(403, "Invalid YouTube WebSub signature")


@app.get("/youtube/websub", include_in_schema=False)
async def verify_youtube_websub(request: Request):
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    mode = request.query_params.get("hub.mode", "")
    topic = request.query_params.get("hub.topic", "")
    challenge = request.query_params.get("hub.challenge", "")
    channel_id = _youtube_channel_from_topic(topic)
    if mode not in {"subscribe", "unsubscribe"} or not challenge or not channel_id:
        raise HTTPException(400, "Invalid YouTube WebSub verification")
    keys = bot_ref.config.get("keys", {})
    secret = keys.get("youtube_websub_secret")
    if not secret:
        raise HTTPException(503, "YouTube WebSub is not configured")
    supplied_token = request.query_params.get("hub.verify_token", "")
    expected_token = youtube_websub_verify_token(str(secret), channel_id)
    if not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(403, "Invalid YouTube WebSub verification token")
    known = await bot_ref.pool.fetchval(
        "SELECT 1 FROM youtube_websub_subscriptions "
        "WHERE youtube_channel_id = $1 "
        "UNION ALL SELECT 1 FROM youtube_follows "
        "WHERE youtube_channel_id = $1 LIMIT 1",
        channel_id,
    )
    if not known:
        raise HTTPException(404, "Unknown YouTube subscription")
    if mode == "unsubscribe":
        await bot_ref.pool.execute(
            "DELETE FROM youtube_websub_subscriptions " "WHERE youtube_channel_id = $1",
            channel_id,
        )
    else:
        try:
            lease_seconds = min(
                864000,
                max(
                    60,
                    int(request.query_params.get("hub.lease_seconds", "864000")),
                ),
            )
        except ValueError:
            raise HTTPException(400, "Invalid YouTube WebSub lease")
        await bot_ref.pool.execute(
            "INSERT INTO youtube_websub_subscriptions "
            "(youtube_channel_id, status, lease_expires_at, updated_at, last_error) "
            "VALUES ($1, 'enabled', now() + ($2 * interval '1 second'), now(), NULL) "
            "ON CONFLICT (youtube_channel_id) DO UPDATE SET status = 'enabled', "
            "lease_expires_at = EXCLUDED.lease_expires_at, updated_at = now(), "
            "last_error = NULL",
            channel_id,
            lease_seconds,
        )
    return PlainTextResponse(challenge)


@app.post("/youtube/websub", include_in_schema=False)
async def youtube_websub(request: Request):
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "YouTube WebSub payload is too large")
    _verify_youtube_websub_signature(
        body,
        request.headers.get("X-Hub-Signature-256")
        or request.headers.get("X-Hub-Signature"),
    )
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise HTTPException(400, "Unsafe YouTube WebSub XML")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise HTTPException(400, "Invalid YouTube WebSub XML")
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    events_cog: Any = bot_ref.get_cog("Events")
    if events_cog is None or not hasattr(events_cog, "process_youtube_event"):
        raise HTTPException(503, "YouTube event handler is not ready")
    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    inserted: list[str] = []
    body_hash = hashlib.sha256(body).hexdigest()
    for entry in root.findall("atom:entry", namespaces):
        video_id = (entry.findtext("yt:videoId", "", namespaces) or "").strip()
        channel_id = (entry.findtext("yt:channelId", "", namespaces) or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not re.fullmatch(
            r"UC[A-Za-z0-9_-]{22}", channel_id
        ):
            continue
        followed = await bot_ref.pool.fetchval(
            "SELECT 1 FROM youtube_follows WHERE youtube_channel_id = $1 LIMIT 1",
            channel_id,
        )
        if not followed:
            continue
        updated = entry.findtext("atom:updated", "", namespaces) or ""
        event_id = hashlib.sha256(
            f"{body_hash}:{channel_id}:{video_id}:{updated}".encode()
        ).hexdigest()
        payload = {
            "channel_id": channel_id,
            "video_id": video_id,
            "title": entry.findtext("atom:title", "", namespaces) or "",
            "published": entry.findtext("atom:published", "", namespaces) or "",
            "updated": updated,
        }
        result = await bot_ref.pool.execute(
            "INSERT INTO youtube_events "
            "(event_id, youtube_channel_id, video_id, payload) "
            "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (event_id) DO NOTHING",
            event_id,
            channel_id,
            video_id,
            payload,
        )
        if result != "INSERT 0 0":
            inserted.append(event_id)
    for event_id in inserted:
        task = asyncio.create_task(events_cog.process_youtube_event(event_id))
        bot_ref._eventsub_tasks.add(task)
        task.add_done_callback(bot_ref._eventsub_tasks.discard)
    return {"ok": True, "accepted": len(inserted)}


async def _check_opted_out(user_id: int) -> bool:
    pool = _check_pool()
    r = await pool.fetchval(
        """
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM user_settings
            WHERE user_id = $1 AND tracking_enabled = FALSE
        )
        OR EXISTS (
            SELECT 1
            FROM opted_out
            WHERE user_id = $1 AND cardinality(items) > 0
        )
        """,
        user_id,
    )
    return r is not None


async def _tracking_opted_out(user_id: int, item: str) -> bool:
    return bool(
        await _check_pool().fetchval(
            """
            SELECT
                COALESCE(
                    (
                        SELECT NOT tracking_enabled
                        FROM user_settings
                        WHERE user_id = $1
                    ),
                    FALSE
                )
                OR COALESCE(
                    (
                        SELECT $2 = ANY(items)
                        FROM opted_out
                        WHERE user_id = $1
                    ),
                    FALSE
                )
            """,
            user_id,
            item,
        )
    )


async def _history_visible_to(
    user_id: int,
    authorization: str | None,
    session_id: str | None,
) -> None:
    # Bot accounts do not have user-controlled privacy settings.  Resolve the
    # account once through Discord and cache the result so their saved history
    # remains viewable without changing any website wording.
    if bot_ref is not None and user_id not in bot_ref.db_cache.known_non_bot_users:
        cached_user = bot_ref.get_user(user_id)
        if cached_user is None and user_id not in _discord_user_negative_cache:
            try:
                cached_user = await bot_ref.fetch_user(user_id)
            except (discord.HTTPException, discord.NotFound, asyncio.TimeoutError):
                # Invalid IDs and transient Discord failures should not cause a
                # fresh API request on every public history lookup.  Keep this
                # negative result short-lived so a newly-created user can still
                # be resolved later.
                _discord_user_negative_cache[user_id] = True
                cached_user = None
        if cached_user is not None:
            bot_ref.db_cache.remember_user(cached_user.id, is_bot=cached_user.bot)
            if cached_user.bot:
                return

    history_public = await _check_pool().fetchval(
        "SELECT history_public FROM user_settings WHERE user_id = $1",
        user_id,
    )
    # Saved history is private unless the owner explicitly enables public
    # visibility.  This also protects users who have no user_settings row yet.
    if history_public is True:
        return

    viewer_id: int | None = None
    if session_id:
        # A valid opaque session already identifies its owner; avoid another
        # Discord API request for this read-only visibility check.
        viewer_id = await _check_pool().fetchval(
            """
            SELECT user_id
            FROM web_sessions
            WHERE session_id_hash = $1 AND expires_at > now()
            """,
            _session_hash(session_id),
        )
    # If a browser retained an expired/stale cookie, a valid bearer token must
    # still be honored for API clients rather than being masked by that cookie.
    if viewer_id is None and authorization:
        try:
            viewer = await _verify_token(authorization, None)
        except HTTPException:
            viewer = None
        if viewer is not None:
            viewer_id = int(viewer["id"])

    if viewer_id != user_id:
        raise HTTPException(403, "This user has made their saved history private")


VALID_OPTOUTS = {
    "avatar",
    "username",
    "display",
    "nickname",
    "discrim",
    "stag",
    "joins",
    "xp",
    "commands",
    "status",
    "activity",
    "pokemon",
    "corn",
    "emoji",
    "downloads",
    "higher_lower",
    "heads_tails",
    # Aggregate controls exposed by the Discord settings dropdown.  Their
    # durable columns live in user_settings/reaction_tracking rather than the
    # per-category opted_out array, but the API presents one unified list.
    "games",
    "currency",
    "reactions",
}


@app.get("/user/{user_id}/opted-out")
async def get_opted_out(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get the list of tracking methods this user has opted out of."""
    await _require_self(user_id, authorization, session_id)
    pool = _check_pool()
    row = await pool.fetchrow("SELECT items FROM opted_out WHERE user_id = $1", user_id)
    items = list(row["items"] if row else [])
    settings = await pool.fetchrow(
        "SELECT game_tracking_enabled, currency_tracking_enabled FROM user_settings WHERE user_id = $1",
        user_id,
    )
    if settings and settings.get("game_tracking_enabled") is False:
        items.append("games")
    if settings and settings.get("currency_tracking_enabled") is False:
        items.append("currency")
    reaction = await pool.fetchval(
        "SELECT enabled FROM reaction_tracking WHERE user_id = $1", user_id
    )
    if reaction is not True:
        items.append("reactions")
    return {"items": items}


@app.post("/user/{user_id}/opted-out")
async def set_opted_out(
    user_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Set the opted-out tracking methods. Requires OAuth."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only manage your own settings")

    items = [i for i in payload.get("items", []) if i in VALID_OPTOUTS]
    ordinary = [i for i in items if i not in {"games", "currency", "reactions"}]
    games_enabled = "games" not in items
    currency_enabled = "currency" not in items
    reactions_enabled = "reactions" not in items
    pool = _check_pool()
    await pool.execute(
        "INSERT INTO opted_out (user_id, items) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET items = $2",
        user_id,
        ordinary,
    )
    await pool.execute(
        "INSERT INTO user_settings (user_id, game_tracking_enabled, currency_tracking_enabled) "
        "VALUES ($1, $2, $3) ON CONFLICT (user_id) DO UPDATE SET "
        "game_tracking_enabled = EXCLUDED.game_tracking_enabled, "
        "currency_tracking_enabled = EXCLUDED.currency_tracking_enabled",
        user_id,
        games_enabled,
        currency_enabled,
    )
    await pool.execute(
        "INSERT INTO reaction_tracking (user_id, enabled) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
        user_id,
        reactions_enabled,
    )

    if bot_ref:
        if ordinary:
            bot_ref.db_cache.opted_out[user_id] = ordinary
        else:
            bot_ref.db_cache.opted_out.pop(user_id, None)
        bot_ref.db_cache.set_game_tracking_enabled(user_id, games_enabled)
        bot_ref.db_cache.set_currency_tracking_enabled(user_id, currency_enabled)
        if reactions_enabled:
            bot_ref.db_cache.enable_reaction_tracking(user_id)
        else:
            bot_ref.db_cache.disable_reaction_tracking(user_id)

    return {"items": items}


@app.get("/user/{user_id}/privacy-settings")
async def get_user_privacy_settings(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get the authenticated user's global tracking and history settings."""
    await _require_self(user_id, authorization, session_id)
    row = await _check_pool().fetchrow(
        """
        SELECT tracking_enabled, history_public, game_tracking_enabled,
               game_history_public, currency_tracking_enabled
        FROM user_settings
        WHERE user_id = $1
        """,
        user_id,
    )
    return {
        "tracking_enabled": row["tracking_enabled"] if row else True,
        "history_public": row["history_public"] if row else False,
        "game_tracking_enabled": row["game_tracking_enabled"] if row else True,
        "game_history_public": row["game_history_public"] if row else True,
        "currency_tracking_enabled": row["currency_tracking_enabled"] if row else True,
    }


@app.post("/user/{user_id}/privacy-settings")
async def set_user_privacy_settings(
    user_id: int,
    payload: dict = Body(...),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Update global tracking and saved-history visibility without deleting data."""
    await _require_self(user_id, authorization, session_id)
    existing = await _check_pool().fetchrow(
        "SELECT tracking_enabled, history_public, game_history_public FROM user_settings WHERE user_id = $1",
        user_id,
    )
    tracking_enabled = payload.get(
        "tracking_enabled", existing["tracking_enabled"] if existing else True
    )
    history_public = payload.get(
        "history_public", existing["history_public"] if existing else False
    )
    if not isinstance(tracking_enabled, bool) or not isinstance(history_public, bool):
        raise HTTPException(
            400,
            "tracking_enabled and history_public must both be booleans",
        )
    await _check_pool().execute(
        """
        INSERT INTO user_settings (
            user_id, tracking_enabled, history_public, game_history_public, tracking_consent
        )
        VALUES ($1, $2, $3, $3, $3)
        ON CONFLICT (user_id) DO UPDATE
        SET tracking_enabled = EXCLUDED.tracking_enabled,
            history_public = EXCLUDED.history_public,
            game_history_public = EXCLUDED.game_history_public,
            tracking_consent = CASE
                WHEN EXCLUDED.history_public THEN TRUE
                ELSE user_settings.tracking_consent
            END
        """,
        user_id,
        tracking_enabled,
        history_public,
    )
    if bot_ref:
        if tracking_enabled:
            bot_ref.db_cache.tracking_disabled_users.discard(user_id)
        else:
            bot_ref.db_cache.tracking_disabled_users.add(user_id)
        bot_ref.db_cache.set_history_public(user_id, history_public)
        if history_public:
            bot_ref.db_cache.set_tracking_consent(user_id)
    return {
        "tracking_enabled": tracking_enabled,
        "history_public": history_public,
    }


async def _verify_token(
    authorization: str | None = None, session_id: str | None = None
) -> dict:
    if session_id:
        token = await _session_access_token(session_id)
    elif authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        raise HTTPException(401, "Missing access token")
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with bot_ref.session.get(
            "https://discord.com/api/users/@me",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise HTTPException(401, "Invalid access token")
            return await resp.json()
    except HTTPException:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise HTTPException(
            503, "Discord authentication is temporarily unavailable"
        ) from error


async def _require_self(
    user_id: int, authorization: str | None, session_id: str | None
) -> dict[str, Any]:
    user = await _verify_token(authorization, session_id)
    if int(user["id"]) != user_id:
        raise HTTPException(403, "You can only access your own data")
    return user


async def _require_guild_manager(
    guild_id: int, authorization: str | None, session_id: str | None
) -> tuple[dict[str, Any], discord.Guild, discord.Member]:
    user = await _verify_token(authorization, session_id)
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    guild = bot_ref.get_guild(guild_id)
    if guild is None:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(user["id"]))
    if member is None:
        try:
            member = await guild.fetch_member(int(user["id"]))
        except (discord.NotFound, discord.Forbidden):
            raise HTTPException(403, "You need Manage Server in this guild")
        except discord.HTTPException as error:
            raise HTTPException(503, "Discord membership lookup failed") from error
    if not member.guild_permissions.manage_guild:
        raise HTTPException(403, "You need Manage Server in this guild")
    return user, guild, member


VALID_GUILD_OPTOUTS = {
    "avatar",
    "icon",
    "name",
    "avatars",
    "icons",
    "names",
    "joins",
    "status",
    "commands",
    "emoji",
    "downloads",
    "corn",
    "reactions",
    "tags",
    "mudae",
}


@app.get("/user/{user_id}/guilds")
async def get_user_guilds(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get guilds where the user has Manage Server. Requires OAuth."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own guilds")
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")

    pool = _check_pool()
    manageable = [
        guild
        for guild in bot_ref.guilds
        if (member := guild.get_member(user_id))
        and member.guild_permissions.manage_guild
    ]
    opt_out_rows = await pool.fetch(
        "SELECT guild_id, items FROM guild_opted_out WHERE guild_id = ANY($1::BIGINT[])",
        [guild.id for guild in manageable],
    )
    opted_out_by_guild = {row["guild_id"]: row["items"] for row in opt_out_rows}
    guilds = []
    for guild in manageable:
        guilds.append(
            {
                "id": str(guild.id),
                "name": guild.name,
                # Use a static icon for the dashboard. Animated Discord
                # GIF frames can leave transparent-frame ghosting in the
                # browser while the image is being updated.
                "icon": str(guild.icon.with_format("png")) if guild.icon else None,
                "opted_out": opted_out_by_guild.get(guild.id, []),
            }
        )

    guilds.sort(key=lambda g: g["name"].lower())
    return {"guilds": guilds}


@app.get("/guild/{guild_id}/opted-out")
async def get_guild_opted_out(
    guild_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get opted-out tracking items for a guild."""
    await _require_guild_manager(guild_id, authorization, session_id)
    pool = _check_pool()
    row = await pool.fetchrow(
        "SELECT items FROM guild_opted_out WHERE guild_id = $1", guild_id
    )
    settings = await pool.fetchrow(
        "SELECT tracking_enabled, history_public FROM guild_settings WHERE guild_id = $1",
        guild_id,
    )
    return {
        "items": row["items"] if row else [],
        "tracking_enabled": settings["tracking_enabled"] if settings else True,
        "history_public": settings["history_public"] if settings else True,
    }


@app.get("/user/{user_id}/highlights")
async def get_user_highlights(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Return a user's highlights for every guild they share with Fishie."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own highlights")
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")

    # Member intent chunks guilds during startup. Avoid issuing one Discord API
    # request per uncached guild from a dashboard request; that pattern does not
    # scale and can exhaust the bot's global rate limit.
    shared_guilds = [
        guild for guild in bot_ref.guilds if guild.get_member(user_id) is not None
    ]
    guild_ids = [guild.id for guild in shared_guilds]
    rows: list[Any] = []
    if guild_ids:
        rows = await _check_pool().fetch(
            "SELECT guild_id, word FROM highlights "
            "WHERE user_id = $1 AND guild_id = ANY($2::BIGINT[]) "
            "ORDER BY guild_id, created_at, word_normalized",
            user_id,
            guild_ids,
        )
    words_by_guild: dict[int, list[str]] = {guild_id: [] for guild_id in guild_ids}
    for row in rows:
        words_by_guild[int(row["guild_id"])].append(str(row["word"]))
    return {
        "guilds": [
            {
                "id": str(guild.id),
                "name": guild.name,
                "icon": str(guild.icon.with_format("png")) if guild.icon else None,
                "highlights": words_by_guild[guild.id],
            }
            for guild in sorted(shared_guilds, key=lambda item: item.name.casefold())
        ]
    }


@app.post("/user/{user_id}/highlights")
async def set_user_highlights(
    user_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Replace a user's highlights for one shared guild."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only manage your own highlights")
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    raw_guild_id = payload.get("guild_id")
    if not isinstance(raw_guild_id, (str, int)):
        raise HTTPException(400, "guild_id must be a guild ID")
    try:
        guild_id = int(raw_guild_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "guild_id must be a guild ID")
    guild: Any = bot_ref.get_guild(guild_id)
    if guild is None or guild.get_member(user_id) is None:
        raise HTTPException(403, "You must share that server with Fishie")

    raw_words = payload.get("words", [])
    if not isinstance(raw_words, list):
        raise HTTPException(400, "words must be a list")
    if len(raw_words) > 100:
        raise HTTPException(400, "You can have up to 100 highlights per server")
    words: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_word in raw_words:
        if not isinstance(raw_word, str):
            continue
        word = " ".join(raw_word.split())
        normalized = word.casefold()
        if not word or len(word) > 100 or normalized in seen:
            continue
        seen.add(normalized)
        words.append((word, normalized))

    pool = _check_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "DELETE FROM highlights WHERE user_id = $1 AND guild_id = $2",
                user_id,
                guild_id,
            )
            if words:
                await connection.executemany(
                    "INSERT INTO highlights "
                    "(user_id, guild_id, word, word_normalized) VALUES ($1, $2, $3, $4)",
                    [
                        (user_id, guild_id, word, normalized)
                        for word, normalized in words
                    ],
                )
    tools: Any = bot_ref.get_cog("Tools")
    if tools is not None and hasattr(tools, "_invalidate_highlights"):
        tools._invalidate_highlights(guild_id)
    return {"guild_id": str(guild_id), "highlights": [word for word, _ in words]}


@app.post("/guild/{guild_id}/opted-out")
async def set_guild_opted_out(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Set opted-out tracking for a guild. Requires OAuth + Manage Server."""
    me = await _verify_token(authorization, session_id)
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")

    guild = bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "You need Manage Server permission in this guild")

    items = [i for i in payload.get("items", []) if i in VALID_GUILD_OPTOUTS]
    tracking_enabled = payload.get("tracking_enabled", True)
    history_public = payload.get("history_public", True)
    if not isinstance(tracking_enabled, bool) or not isinstance(history_public, bool):
        raise HTTPException(400, "tracking_enabled and history_public must be booleans")
    pool = _check_pool()
    await pool.execute(
        "INSERT INTO guild_opted_out (guild_id, items) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET items = $2",
        guild_id,
        items,
    )
    await pool.execute(
        "INSERT INTO guild_settings (guild_id, tracking_enabled, history_public) "
        "VALUES ($1, $2, $3) ON CONFLICT (guild_id) DO UPDATE SET "
        "tracking_enabled = EXCLUDED.tracking_enabled, "
        "history_public = EXCLUDED.history_public",
        guild_id,
        tracking_enabled,
        history_public,
    )

    if bot_ref:
        if items:
            bot_ref.db_cache.opted_out[guild_id] = items
        else:
            bot_ref.db_cache.opted_out.pop(guild_id, None)
        bot_ref.db_cache.set_guild_tracking_enabled(guild_id, tracking_enabled)
        bot_ref.db_cache.set_guild_history_public(guild_id, history_public)

    return {
        "items": items,
        "tracking_enabled": tracking_enabled,
        "history_public": history_public,
    }


async def _refresh_urls(urls: list[str]) -> list[str]:
    """Call Discord's refresh-urls endpoint to get fresh CDN links."""
    if not urls or not bot_ref:
        return urls
    clean = list({u.split("?")[0] for u in urls if u})
    if not clean:
        return urls
    mapping: dict[str, str] = {}
    BATCH_SIZE = 50
    for i in range(0, len(clean), BATCH_SIZE):
        batch = clean[i : i + BATCH_SIZE]
        try:
            req = await bot_ref.http.request(
                __import__("discord").http.Route("POST", "/attachments/refresh-urls"),
                json={"attachment_urls": batch},
            )
            for item in req.get("refreshed_urls", []):
                orig = item.get("original", "")
                refreshed = item.get("refreshed", "")
                if orig and refreshed:
                    mapping[orig] = refreshed
        except Exception:
            continue
    return [mapping.get(u.split("?")[0], u) for u in urls]


@app.get("/guild/{guild_id}/icons")
async def get_guild_icons(
    guild_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(80, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get guild icon history."""
    await _require_guild_manager(guild_id, authorization, session_id)
    pool = _check_pool()
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM guild_icons WHERE guild_id = $1", guild_id
    )
    pages = max((count + per_page - 1) // per_page, 1)
    offset = (page - 1) * per_page
    rows = await pool.fetch(
        "SELECT icon_key, icon, created_at FROM guild_icons WHERE guild_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        guild_id,
        per_page,
        offset,
    )
    urls = [r["icon"] for r in rows if r["icon"]]
    refreshed = await _refresh_urls(urls)
    url_map = dict(zip(urls, refreshed, strict=False))
    icons = [
        {
            "icon_key": r["icon_key"],
            "url": url_map.get(r["icon"], r["icon"]),
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
    return {"items": icons, "total": count, "page": page, "pages": pages}


@app.get("/guild/{guild_id}/names")
async def get_guild_names(
    guild_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(80, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get guild name history."""
    await _require_guild_manager(guild_id, authorization, session_id)
    pool = _check_pool()
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM guild_name_logs WHERE guild_id = $1", guild_id
    )
    pages = max((count + per_page - 1) // per_page, 1)
    offset = (page - 1) * per_page
    rows = await pool.fetch(
        "SELECT id, name, created_at FROM guild_name_logs WHERE guild_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        guild_id,
        per_page,
        offset,
    )
    names = [
        {"id": r["id"], "value": r["name"], "created_at": r["created_at"].isoformat()}
        for r in rows
    ]
    return {"items": names, "total": count, "page": page, "pages": pages}


@app.get("/commands")
async def list_commands():
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    cmds = []

    def is_effect_app_subcommand(command: Any) -> bool:
        """Keep the split effect groups, but hide their child commands on the site."""
        qualified_name = str(getattr(command, "qualified_name", "")).casefold()
        return bool(re.match(r"^effect(?:-\d+)?\s+", qualified_name))

    def add_cmd(c):
        if (
            c.hidden
            or c.cog_name in ("Owner", "Jishaku")
            or is_effect_app_subcommand(c)
        ):
            return
        aliases = ", ".join(c.aliases) if c.aliases else ""
        params = []
        for name, param in c.clean_params.items():
            req = "required" if param.default is param.empty else "optional"
            params.append({"name": name, "required": req})
        extras = getattr(c, "extras", {})
        category = extras.get("help_category") if isinstance(extras, dict) else None
        if not category and str(getattr(c, "qualified_name", "")).casefold().startswith(
            "game "
        ):
            category = "Games"
        cmds.append(
            {
                "name": c.qualified_name,
                "description": c.description or c.short_doc or "",
                "category": category or c.cog_name or "Uncategorized",
                "usage": c.usage or "",
                "aliases": aliases,
                "params": params,
            }
        )

    for cmd in bot_ref.commands:
        add_cmd(cmd)
        if hasattr(cmd, "walk_commands"):
            for sub in cast(Any, cmd).walk_commands():
                add_cmd(sub)
    return {"commands": sorted(cmds, key=lambda c: (c["category"], c["name"]))}


_stats_cache: tuple[float, dict[str, Any]] | None = None
_stats_lock = asyncio.Lock()


@app.get("/stats")
async def bot_stats():
    global _stats_cache
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    now_monotonic = time.monotonic()
    if _stats_cache and now_monotonic - _stats_cache[0] < 300:
        return _stats_cache[1]
    import datetime

    async with _stats_lock:
        now_monotonic = time.monotonic()
        if _stats_cache and now_monotonic - _stats_cache[0] < 300:
            return _stats_cache[1]
        pool = _check_pool()
        async with pool.acquire() as conn:
            start = datetime.datetime.now(datetime.timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            rows = await conn.fetch(
                """
                SELECT 'avatars' AS name, COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE created_at >= $1) AS today FROM avatars
                UNION ALL SELECT 'commands', COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= $1) FROM command_logs
                UNION ALL SELECT 'usernames', COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= $1) FROM username_logs
                UNION ALL SELECT 'discrims', COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= $1) FROM discrim_logs
                UNION ALL SELECT 'nicknames', COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= $1) FROM nickname_logs
                UNION ALL SELECT 'guild_names', COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= $1) FROM guild_name_logs
                UNION ALL SELECT 'member_joins', COUNT(*),
                    COUNT(*) FILTER (WHERE time >= $1) FROM member_join_logs
                UNION ALL SELECT 'guild_icons', COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= $1) FROM guild_icons
                UNION ALL SELECT 'guild_avatars', COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= $1) FROM guild_avatars
                """,
                start,
            )
        by_name = {row["name"]: row for row in rows}
        result = {
            "guilds": len(bot_ref.guilds),
            "users": sum(g.member_count or 0 for g in bot_ref.guilds),
            "commands": len(bot_ref.commands),
            "uptime_seconds": (
                (
                    datetime.datetime.now().astimezone() - bot_ref.start_time
                ).total_seconds()
                if hasattr(bot_ref, "start_time")
                else 0
            ),
            "today": {name: row["today"] for name, row in by_name.items()},
            "totals": {name: row["total"] for name, row in by_name.items()},
        }
        _stats_cache = (now_monotonic, result)
        return result


class OAuthExchangePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=32, max_length=256)
    redirect_uri: str = "https://crygup.com/dashboard"


def _oauth_redirect_uri(value: str) -> str:
    if value not in {"https://crygup.com", "https://crygup.com/dashboard"}:
        raise HTTPException(400, "Invalid OAuth redirect URI")
    return value


@app.get("/oauth/start")
async def oauth_start(
    response: Response,
    redirect_uri: str = Query("https://crygup.com/dashboard"),
):
    """Create a one-time Discord OAuth state and PKCE verifier."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    redirect_uri = _oauth_redirect_uri(redirect_uri)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    pool = _check_pool()
    await pool.execute("DELETE FROM oauth_states WHERE expires_at <= now()")
    await pool.execute(
        "INSERT INTO oauth_states (state_hash, code_verifier, redirect_uri, expires_at) "
        "VALUES ($1, $2, $3, now() + interval '10 minutes')",
        _session_hash(state),
        verifier,
        redirect_uri,
    )
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        max_age=OAUTH_STATE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    query = urlencode(
        {
            "client_id": str(_active_application_id()),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return {"url": f"https://discord.com/oauth2/authorize?{query}"}


@app.post("/oauth/exchange")
async def oauth_exchange(
    response: Response,
    payload: OAuthExchangePayload,
    oauth_state: str | None = Cookie(None, alias=OAUTH_STATE_COOKIE),
):
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    redirect_uri = _oauth_redirect_uri(payload.redirect_uri)
    if not oauth_state or not hmac.compare_digest(oauth_state, payload.state):
        raise HTTPException(400, "Invalid OAuth state")
    row = await _check_pool().fetchrow(
        "DELETE FROM oauth_states WHERE state_hash = $1 AND expires_at > now() "
        "RETURNING code_verifier, redirect_uri",
        _session_hash(payload.state),
    )
    if row is None or row["redirect_uri"] != redirect_uri:
        raise HTTPException(400, "Invalid or expired OAuth state")
    data = {
        "client_id": str(_active_application_id()),
        "client_secret": _active_client_secret(),
        "code": payload.code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": row["code_verifier"],
    }
    async with bot_ref.session.post(
        "https://discord.com/api/oauth2/token", data=data
    ) as resp:
        if resp.status != 200:
            raise HTTPException(400, "OAuth exchange failed")
        token_data = await resp.json()
    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    async with bot_ref.session.get(
        "https://discord.com/api/users/@me", headers=headers
    ) as resp:
        if resp.status != 200:
            raise HTTPException(400, "Discord user lookup failed")
        user_data = await resp.json()
    session_id, max_age = await _create_web_session(
        int(user_data["id"]),
        token_data["access_token"],
        token_data.get("expires_in"),
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(key=OAUTH_STATE_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"user": user_data}


@app.post("/oauth/logout")
async def oauth_logout(
    response: Response,
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    if session_id:
        pool = _check_pool()
        await pool.execute(
            "DELETE FROM web_sessions WHERE session_id_hash = $1",
            _session_hash(session_id),
        )
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}


@app.get("/oauth/me")
async def oauth_me(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Return the current session, treating a missing/expired login as anonymous."""
    response.headers["Cache-Control"] = "private, no-store"
    if not session_id and not (authorization and authorization.startswith("Bearer ")):
        return {"authenticated": False, "user": None}

    try:
        user = await _verify_token(authorization, session_id)
    except HTTPException as error:
        if error.status_code == 401:
            if session_id:
                response.delete_cookie(key=SESSION_COOKIE, path="/")
            return {"authenticated": False, "user": None}
        raise
    return {"authenticated": True, "user": user}


@app.get("/lastfm/connect")
async def lastfm_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Create a Last.fm authorization URL for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    # Bind the OAuth redirect to this browser.  The nonce is returned only in
    # the HttpOnly cookie; the callback must present both it and the original
    # Fishie session before the one-time state can be consumed.
    browser_nonce = secrets.token_urlsafe(32)
    state_url = _lastfm_authorization_url(
        int(me["id"]),
        "website",
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    response.set_cookie(
        key=LASTFM_STATE_COOKIE,
        value=browser_nonce,
        max_age=LASTFM_STATE_TTL,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return {"url": state_url}


@app.get("/lastfm/callback")
async def lastfm_callback(
    response: Response,
    token: str = Query(...),
    state: str = Query(...),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=LASTFM_STATE_COOKIE),
):
    """Exchange a Last.fm callback token and persist the verified account."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise HTTPException(400, "Invalid Last.fm authentication token")

    user_id, source, channel_id, message_id = _decode_lastfm_state(
        state,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    api_key = bot_ref.config["keys"]["lastfm_cb"]
    api_secret = bot_ref.config["keys"]["lastfm_cb_secret"]
    payload = {
        "method": "auth.getSession",
        "api_key": api_key,
        "token": token,
        "api_sig": _lastfm_api_signature(api_key, token, api_secret),
        "format": "json",
    }
    async with bot_ref.session.post(LASTFM_API_URL, data=payload) as resp:
        try:
            result = await resp.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError):
            raise HTTPException(502, "Last.fm returned an invalid response")

    session = result.get("session") if isinstance(result, dict) else None
    if resp.status != 200 or not isinstance(session, dict):
        message = (
            result.get("message", "Last.fm authorization failed")
            if isinstance(result, dict)
            else "Last.fm authorization failed"
        )
        raise HTTPException(400, str(message))
    username = session.get("name")
    session_key = session.get("key")
    if not username or not session_key:
        raise HTTPException(502, "Last.fm did not return account credentials")

    pool = _check_pool()
    await pool.execute(
        """INSERT INTO accounts (user_id, lastfm, lastfm_session_key)
           VALUES ($1, $2, $3)
           ON CONFLICT (user_id) DO UPDATE
           SET lastfm = EXCLUDED.lastfm,
               lastfm_session_key = EXCLUDED.lastfm_session_key;
        """,
        user_id,
        username,
        encrypt_credential(session_key),
    )
    await bot_ref.refresh_account_cache(user_id)
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    response.delete_cookie(key=LASTFM_STATE_COOKIE, path="/")
    return {"username": username, "source": source}


@app.get("/steam/connect")
async def steam_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Create a Steam OpenID URL for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    if not session_id:
        raise HTTPException(401, "A browser session is required to connect Steam")
    browser_nonce = secrets.token_urlsafe(32)
    response.headers["Cache-Control"] = "private, no-store"
    response.set_cookie(
        key=STEAM_STATE_COOKIE,
        value=browser_nonce,
        max_age=STEAM_STATE_TTL,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {
        "url": _steam_authorization_url(
            int(me["id"]),
            "website",
            session_id=session_id,
            browser_nonce=browser_nonce,
        )
    }


@app.get("/steam/callback")
async def steam_callback(
    response: Response,
    request: Request,
    steam_state: str = Query(...),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=STEAM_STATE_COOKIE),
):
    """Verify a Steam OpenID response and persist the user's SteamID64."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_steam_state(
        steam_state,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    openid = {
        key: value
        for key, value in request.query_params.items()
        if key.startswith("openid.")
    }
    if openid.get("openid.mode") != "id_res":
        raise HTTPException(400, "Steam authorization was cancelled")
    return_to = openid.get("openid.return_to")
    if not return_to:
        raise HTTPException(400, "Steam did not return a callback URL")
    parsed_return_to = urlsplit(return_to)
    if (
        parsed_return_to.scheme != "https"
        or parsed_return_to.netloc != "crygup.com"
        or parsed_return_to.path != "/fishie"
        or parse_qs(parsed_return_to.query).get("steam_state") != [steam_state]
    ):
        raise HTTPException(400, "Invalid Steam callback URL")
    claimed_id = openid.get("openid.claimed_id", "")
    identity = openid.get("openid.identity", "")
    match = re.fullmatch(r"https://steamcommunity\.com/openid/id/(\d{17})", claimed_id)
    if not match or identity != claimed_id:
        raise HTTPException(400, "Steam returned an invalid account identifier")

    verify_payload = dict(openid)
    verify_payload["openid.mode"] = "check_authentication"
    async with bot_ref.session.post(STEAM_OPENID_URL, data=verify_payload) as resp:
        verification = await resp.text()
    if resp.status != 200 or not re.search(
        r"(?:^|\n)is_valid:true(?:\r?\n|$)", verification
    ):
        raise HTTPException(400, "Steam authorization could not be verified")

    steam_id = match.group(1)
    persona_name = None
    api_key = bot_ref.config["keys"].get("steam")
    if api_key:
        try:
            async with bot_ref.session.get(
                "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                params={"key": api_key, "steamids": steam_id, "format": "json"},
            ) as profile_resp:
                profile = await profile_resp.json(content_type=None)
            response_data = (
                profile.get("response") if isinstance(profile, dict) else None
            )
            players = (
                response_data.get("players", [])
                if isinstance(response_data, dict)
                else []
            )
            if isinstance(players, list) and players and isinstance(players[0], dict):
                persona_name = players[0].get("personaname")
        except (aiohttp.ClientError, ValueError, TypeError):
            bot_ref.logger.warning("Steam profile lookup failed for linked account")

    pool = _check_pool()
    await pool.execute(
        """INSERT INTO accounts (user_id, steam)
           VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE
           SET steam = EXCLUDED.steam;""",
        user_id,
        steam_id,
    )
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    response.delete_cookie(key=STEAM_STATE_COOKIE, path="/")
    return {"steamid": steam_id, "personaname": persona_name, "source": source}


@app.get("/spotify/connect")
async def spotify_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Create a Spotify authorization URL for the authenticated user."""
    me = await _verify_token(authorization, session_id)
    if not session_id:
        raise HTTPException(401, "A browser session is required to connect Spotify")
    browser_nonce = secrets.token_urlsafe(32)
    response.set_cookie(
        key=SPOTIFY_STATE_COOKIE,
        value=browser_nonce,
        max_age=SPOTIFY_STATE_TTL,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "url": _spotify_authorization_url(
            int(me["id"]),
            "website",
            session_id=session_id,
            browser_nonce=browser_nonce,
        )
    }


@app.get("/spotify/callback")
async def spotify_callback(
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=SPOTIFY_STATE_COOKIE),
):
    """Exchange a Spotify authorization code and persist the user's account."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_spotify_state(
        state,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    bot_ref.logger.info(
        "Spotify OAuth callback accepted source=%s user_id=%s", source, user_id
    )
    auth = aiohttp.BasicAuth(
        bot_ref.config["keys"]["spotify_id"],
        bot_ref.config["keys"]["spotify_secret"],
    )
    try:
        async with bot_ref.session.post(
            SPOTIFY_TOKEN_URL,
            auth=auth,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": SPOTIFY_CALLBACK_URL,
            },
        ) as token_response:
            try:
                token_data = await token_response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                raise HTTPException(400, "Spotify returned an invalid token response")
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        bot_ref.logger.warning("Spotify token exchange unavailable: %s", error)
        raise HTTPException(400, "Spotify authorization service unavailable") from error
    bot_ref.logger.info(
        "Spotify OAuth token exchange completed status=%s user_id=%s",
        token_response.status,
        user_id,
    )
    if token_response.status != 200 or not isinstance(token_data, dict):
        error_code = token_data.get("error") if isinstance(token_data, dict) else None
        bot_ref.logger.warning(
            "Spotify OAuth token exchange rejected status=%s error=%s user_id=%s",
            token_response.status,
            error_code or "unknown",
            user_id,
        )
        raise HTTPException(400, "Spotify authorization failed")
    access_token = token_data.get("access_token")
    issued_refresh_token = token_data.get("refresh_token")
    bot_ref.logger.info(
        "Spotify OAuth credentials received access=%s refresh=%s user_id=%s",
        bool(access_token),
        bool(issued_refresh_token),
        user_id,
    )
    if not access_token:
        raise HTTPException(400, "Spotify did not return an access token")

    # Spotify may omit refresh_token when an account authorizes the app again.
    # Keep the token already stored for that user instead of rejecting a valid
    # access-token exchange.
    pool = _check_pool()
    refresh_token = issued_refresh_token
    if not refresh_token:
        try:
            stored_refresh_token = await asyncio.wait_for(
                pool.fetchval(
                    "SELECT spotify_refresh_token FROM accounts WHERE user_id = $1",
                    user_id,
                ),
                timeout=5,
            )
            refresh_token = decrypt_credential(stored_refresh_token)
        except asyncio.TimeoutError:
            bot_ref.logger.warning(
                "Spotify stored refresh-token lookup timed out user_id=%s", user_id
            )
            raise HTTPException(400, "Spotify connection temporarily unavailable")
    if not refresh_token:
        bot_ref.logger.warning(
            "Spotify OAuth returned no refresh token and none is stored user_id=%s",
            user_id,
        )
        raise HTTPException(
            400,
            "Spotify did not issue a new refresh token. Remove Fishie from your "
            "Spotify account's Apps page, then connect it again.",
        )

    # Persist the durable credential before any optional profile or Discord
    # work. If a later step fails, the next authorization can reuse this token.
    try:
        await asyncio.wait_for(
            pool.execute(
                """INSERT INTO accounts (user_id, spotify_refresh_token)
                   VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE
                   SET spotify_refresh_token = EXCLUDED.spotify_refresh_token;""",
                user_id,
                encrypt_credential(refresh_token),
            ),
            timeout=5,
        )
    except asyncio.TimeoutError:
        bot_ref.logger.warning(
            "Spotify refresh-token persistence timed out user_id=%s", user_id
        )
        raise HTTPException(400, "Spotify connection temporarily unavailable")
    bot_ref.logger.info("Spotify refresh token persisted user_id=%s", user_id)

    try:
        bot_ref.logger.info("Spotify OAuth profile lookup started user_id=%s", user_id)
        async with bot_ref.session.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as profile_response:
            profile_status = profile_response.status
            profile_content_type = profile_response.headers.get("Content-Type", "")
            profile_body = await profile_response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        bot_ref.logger.warning("Spotify profile lookup unavailable: %s", error)
        raise HTTPException(400, "Spotify profile service unavailable") from error
    bot_ref.logger.info(
        "Spotify OAuth profile response status=%s content_type=%s bytes=%s user_id=%s",
        profile_status,
        profile_content_type.split(";", 1)[0] or "unknown",
        len(profile_body),
        user_id,
    )
    try:
        profile = json.loads(profile_body)
    except (TypeError, ValueError):
        bot_ref.logger.warning(
            "Spotify profile response was not JSON status=%s user_id=%s",
            profile_status,
            user_id,
        )
        raise HTTPException(400, "Spotify returned an invalid profile response")
    if profile_status != 200 or not isinstance(profile, dict):
        raise HTTPException(400, "Spotify profile lookup failed")
    display_name = profile.get("display_name") or profile.get("id")
    if not display_name:
        raise HTTPException(400, "Spotify did not return a display name")

    try:
        await asyncio.wait_for(
            pool.execute(
                """UPDATE accounts
                   SET spotify = $2, spotify_refresh_token = $3
                   WHERE user_id = $1;""",
                user_id,
                display_name,
                encrypt_credential(refresh_token),
            ),
            timeout=5,
        )
    except asyncio.TimeoutError:
        bot_ref.logger.warning(
            "Spotify profile persistence timed out user_id=%s", user_id
        )
        raise HTTPException(400, "Spotify connection temporarily unavailable")
    _schedule_discord_accounts_refresh(user_id, channel_id, message_id)
    bot_ref.logger.info("Spotify account linked user_id=%s", user_id)
    response.delete_cookie(key=SPOTIFY_STATE_COOKIE, path="/")
    return {"display_name": display_name, "source": source}


@app.get("/anilist/connect")
async def anilist_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Create an AniList authorization URL for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    if not session_id:
        raise HTTPException(401, "A browser session is required to connect AniList")
    browser_nonce = secrets.token_urlsafe(32)
    response.set_cookie(
        key=ANILIST_STATE_COOKIE,
        value=browser_nonce,
        max_age=ANILIST_STATE_TTL,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "url": _anilist_authorization_url(
            int(me["id"]),
            "website",
            session_id=session_id,
            browser_nonce=browser_nonce,
        )
    }


@app.get("/anilist/callback")
async def anilist_callback(
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=ANILIST_STATE_COOKIE),
):
    """Exchange an AniList code and persist the verified profile credentials."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_anilist_state(
        state,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    payload = {
        "grant_type": "authorization_code",
        "client_id": bot_ref.config["keys"]["anilist_id"],
        "client_secret": bot_ref.config["keys"]["anilist_secret"],
        "redirect_uri": ANILIST_CALLBACK_URL,
        "code": code,
    }
    async with bot_ref.session.post(ANILIST_TOKEN_URL, json=payload) as token_response:
        try:
            token_data = await token_response.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError):
            raise HTTPException(502, "AniList returned an invalid token response")
    access_token = (
        token_data.get("access_token") if isinstance(token_data, dict) else None
    )
    if token_response.status != 200 or not access_token:
        raise HTTPException(400, "AniList authorization failed")

    query = {"query": "query { Viewer { id name } }"}
    async with bot_ref.session.post(
        ANILIST_GRAPHQL_URL,
        json=query,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as profile_response:
        try:
            profile_data = await profile_response.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError):
            raise HTTPException(502, "AniList returned an invalid profile response")
    profile_payload = (
        profile_data.get("data") if isinstance(profile_data, dict) else None
    )
    viewer = (
        profile_payload.get("Viewer") if isinstance(profile_payload, dict) else None
    )
    username = viewer.get("name") if isinstance(viewer, dict) else None
    if profile_response.status != 200 or not username:
        raise HTTPException(400, "AniList profile lookup failed")

    pool = _check_pool()
    await pool.execute(
        """INSERT INTO accounts (user_id, anilist, anilist_access_token)
           VALUES ($1, $2, $3)
           ON CONFLICT (user_id) DO UPDATE SET
               anilist = EXCLUDED.anilist,
               anilist_access_token = EXCLUDED.anilist_access_token;""",
        user_id,
        username,
        encrypt_credential(access_token),
    )
    await bot_ref.refresh_account_cache(user_id)
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    response.delete_cookie(key=ANILIST_STATE_COOKIE, path="/")
    return {"username": username, "source": source}


@app.get("/user/{user_id}")
async def get_user_data(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = _check_pool()
    counts = await pool.fetchrow(
        """SELECT
               (SELECT COUNT(*) FROM avatars WHERE user_id = $1) AS avatars,
               (SELECT COUNT(*) FROM username_logs WHERE user_id = $1) AS usernames,
               (SELECT COUNT(*) FROM display_name_logs WHERE user_id = $1) AS display_names,
               (SELECT COUNT(*) FROM discrim_logs WHERE user_id = $1) AS discrims,
               (SELECT COUNT(*) FROM stag_logs WHERE user_id = $1) AS server_tags,
               (SELECT COUNT(*) FROM user_status_history WHERE user_id = $1) AS statuses""",
        user_id,
    )
    return {"user_id": user_id, "counts": dict(counts) if counts is not None else {}}


@app.get("/user/{user_id}/xp")
async def get_user_xp(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get XP and message count for a user. Requires OAuth."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own XP")
    pool = _check_pool()
    row = await pool.fetchrow(
        "SELECT messages, xp FROM message_xp WHERE user_id = $1", user_id
    )
    if not row:
        return {"messages": 0, "xp": 0}
    return {"messages": row["messages"], "xp": row["xp"]}


@app.get("/usernames/{user_id}")
async def get_usernames(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM username_logs WHERE user_id = $1", user_id
        )
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            "SELECT id, username, created_at FROM username_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user_id,
            per_page,
            (page - 1) * per_page,
        )
    return {
        "items": [
            {
                "id": r["id"],
                "value": r["username"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "total": count,
        "page": page,
        "pages": pages,
    }


@app.get("/display-names/{user_id}")
async def get_display_names(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM display_name_logs WHERE user_id = $1", user_id
        )
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            "SELECT id, display_name, created_at FROM display_name_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user_id,
            per_page,
            (page - 1) * per_page,
        )
    return {
        "items": [
            {
                "id": r["id"],
                "value": r["display_name"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "total": count,
        "page": page,
        "pages": pages,
    }


@app.get("/discrims/{user_id}")
async def get_discrims(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM discrim_logs WHERE user_id = $1", user_id
        )
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            "SELECT id, discrim, created_at FROM discrim_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user_id,
            per_page,
            (page - 1) * per_page,
        )
    return {
        "items": [
            {
                "id": r["id"],
                "value": r["discrim"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "total": count,
        "page": page,
        "pages": pages,
    }


@app.get("/server-tags/{user_id}")
async def get_server_tags(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get server-tag history when the user permits public history lookup."""
    await _history_visible_to(user_id, authorization, session_id)
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM stag_logs WHERE user_id = $1",
            user_id,
        )
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            """
            SELECT id, tag, guild_id, guild_created_at, badge_url, created_at
            FROM stag_logs
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            user_id,
            per_page,
            (page - 1) * per_page,
        )
    return {
        "items": [
            {
                "id": row["id"],
                "value": (
                    f"{row['tag'] or 'No server tag'}"
                    + (
                        f" • Server {row['guild_id']}"
                        if row["guild_id"] is not None
                        else ""
                    )
                ),
                "tag": row["tag"],
                "guild_id": (
                    str(row["guild_id"]) if row["guild_id"] is not None else None
                ),
                "guild_created_at": (
                    row["guild_created_at"].isoformat()
                    if row["guild_created_at"] is not None
                    else None
                ),
                "badge_url": row["badge_url"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
        "total": count,
        "page": page,
        "pages": pages,
    }


@app.get("/statuses/{user_id}")
async def get_status_history(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get presence history when the user permits public history lookup."""
    await _history_visible_to(user_id, authorization, session_id)
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM user_status_history WHERE user_id = $1",
            user_id,
        )
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            """
            SELECT id, guild_id, status, started_at, ended_at
            FROM user_status_history
            WHERE user_id = $1
            ORDER BY started_at DESC
            LIMIT $2 OFFSET $3
            """,
            user_id,
            per_page,
            (page - 1) * per_page,
        )
    return {
        "items": [
            {
                "id": row["id"],
                "value": f"{row['status'].title()} • Server {row['guild_id']}",
                "status": row["status"],
                "guild_id": str(row["guild_id"]),
                "created_at": row["started_at"].isoformat(),
                "started_at": row["started_at"].isoformat(),
                "ended_at": (
                    row["ended_at"].isoformat() if row["ended_at"] is not None else None
                ),
            }
            for row in rows
        ],
        "total": count,
        "page": page,
        "pages": pages,
    }


@app.delete("/user/{user_id}")
async def delete_user_data(
    user_id: int,
    table: str = Query(None),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    me = await _verify_token(authorization, session_id)

    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only delete your own data")
    deleted = 0
    pool = _check_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if table is None:
                deleted = await erase_user(conn, user_id)
            else:
                tables = [table]
                for t in tables:
                    db_table = TABLE_MAP.get(t)
                    if not db_table or t in GUILD_TABLES:
                        raise HTTPException(400, f"Invalid user table: {t}")
                    r = await conn.execute(
                        f"DELETE FROM {db_table} WHERE user_id = $1", user_id
                    )
                    deleted += int(r.split()[-1])
    if bot_ref:
        await bot_ref.refresh_account_cache(user_id)
        if table is None or table == "user_badges":
            remove_user_badge(user_id)
            bot_ref.db_cache.user_badges.pop(user_id, None)
        bot_ref.db_cache.opted_out.pop(user_id, None)
        bot_ref.cached_mudae_consent.discard(user_id)
        tools = bot_ref.get_cog("Tools")
        if tools is not None and hasattr(tools, "_highlight_cache"):
            cast(Any, tools)._highlight_cache.clear()
        fun = bot_ref.get_cog("Fun")
        if fun is not None and hasattr(fun, "_phone_consent_cache"):
            cast(Any, fun)._phone_consent_cache.pop(user_id, None)
    elif table is None or table == "user_badges":
        # Keep the editable JSON mirror private-data deletion-safe even when
        # the API is running without a bot instance attached.
        remove_user_badge(user_id)
    return {"user_id": user_id, "deleted_rows": deleted}


@app.get("/resolve")
async def resolve_user(
    q: str = Query(..., min_length=2, max_length=64),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Resolve a Discord username or ID to a user ID."""
    await _verify_token(authorization, session_id)
    q = q.strip()
    if q.isdigit():
        return {"user_id": str(q)}
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild_id = int(bot_ref.config.get("ids", {}).get("guild_id", "0") or "0")
    if not guild_id:
        raise HTTPException(400, "Username lookup not available, use a Discord ID")
    guild = bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(502, "Bot is not in the configured guild")
    members = await guild.query_members(q, limit=5)
    for m in members:
        if m.name.lower() == q.lower() or (
            m.global_name and m.global_name.lower() == q.lower()
        ):
            return {"user_id": str(m.id)}
    if members:
        return {"user_id": str(members[0].id)}
    raise HTTPException(
        404, f'No guild member matched "{q}". Try a Discord ID instead.'
    )


@app.delete("/item/{table}/{user_id}")
async def delete_item(
    table: str,
    user_id: int,
    key: str = Query(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Delete a specific logged item. Requires OAuth."""
    me = await _verify_token(authorization, session_id)

    pool = _check_pool()
    async with pool.acquire() as conn:
        db_table = TABLE_MAP.get(table)
        if not db_table:
            raise HTTPException(400, f"Invalid table: {table}")
        if table in GUILD_TABLES:
            if not bot_ref:
                raise HTTPException(503, "Bot not ready")
            guild = bot_ref.get_guild(user_id)
            if not guild:
                raise HTTPException(404, "Guild not found")
            member = guild.get_member(int(me["id"]))
            if not member or not member.guild_permissions.manage_guild:
                raise HTTPException(403, "You need Manage Server in this guild")
            if table == "guild_icons":
                result = await conn.execute(
                    f"DELETE FROM {db_table} WHERE guild_id = $1 AND icon_key = $2",
                    user_id,
                    key,
                )
            else:
                result = await conn.execute(
                    f"DELETE FROM {db_table} WHERE guild_id = $1 AND id = $2",
                    user_id,
                    int(key),
                )
        elif table == "avatars":
            if int(me["id"]) != user_id:
                raise HTTPException(403, "You can only delete your own data")
            result = await conn.execute(
                f"DELETE FROM {db_table} WHERE user_id = $1 AND avatar_key = $2",
                user_id,
                key,
            )
        else:
            if int(me["id"]) != user_id:
                raise HTTPException(403, "You can only delete your own data")
            result = await conn.execute(
                f"DELETE FROM {db_table} WHERE user_id = $1 AND id = $2",
                user_id,
                int(key),
            )
    deleted = int(result.rsplit(" ", 1)[-1])
    return {"deleted": deleted > 0, "deleted_rows": deleted}


_spotify_cover_cache = TTLCache[tuple[str, str], str](maxsize=1024, ttl=600)
_spotify_cover_negative_cache = TTLCache[tuple[str, str], bool](maxsize=4096, ttl=300)
_spotify_cover_token_lock = asyncio.Lock()
_spotify_cover_semaphore = asyncio.Semaphore(4)


def _require_website_origin(request: Request) -> None:
    """Require browser requests to originate from the Fishie website.

    CORS controls whether a browser can read a response; it does not prevent
    arbitrary clients from posting directly.  These public website endpoints
    therefore require the exact origin as an additional request boundary.
    """

    if request.headers.get("origin") not in WEB_ORIGINS:
        raise HTTPException(403, "This endpoint is available from the Fishie website")


def _normalise_spotify_query(value: str) -> str:
    return " ".join(value.casefold().split())


def _check_spotify_cover_rate(ip: str) -> None:
    now = time.monotonic()
    timestamps = [
        timestamp
        for timestamp in (_spotify_cover_rate.get(ip) or [])
        if now - timestamp < SPOTIFY_COVER_RATE_WINDOW
    ]
    if len(timestamps) >= SPOTIFY_COVER_RATE_LIMIT:
        retry_after = max(
            1,
            int(SPOTIFY_COVER_RATE_WINDOW - (now - timestamps[0])) + 1,
        )
        raise HTTPException(
            429,
            "Too many cover lookups; please try again later",
            headers={"Retry-After": str(retry_after)},
        )
    timestamps.append(now)
    _spotify_cover_rate[ip] = timestamps


@app.get("/spotify-cover")
async def spotify_cover(
    request: Request,
    artist: str = Query(..., min_length=1, max_length=200),
    track: str = Query(..., min_length=1, max_length=200),
):
    """Search Spotify for a track cover image. Falls back if Last.fm has no cover."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    _require_website_origin(request)
    ip = _client_ip(request)
    _check_spotify_cover_rate(ip)
    sid = bot_ref.config["keys"]["spotify_id"]
    ss = bot_ref.config["keys"]["spotify_secret"]
    encoded = base64.b64encode(f"{sid}:{ss}".encode("ascii")).decode("ascii")
    cache_key = (_normalise_spotify_query(artist), _normalise_spotify_query(track))
    if cached := _spotify_cover_cache.get(cache_key):
        return {"url": cached}
    if cache_key in _spotify_cover_negative_cache:
        raise HTTPException(404, "No cover found")

    # Limit concurrent Spotify calls and avoid a token stampede when the
    # process starts with no cached client-credentials token.
    async with _spotify_cover_semaphore:
        token = bot_ref.spotify_key
        if token is None:
            async with _spotify_cover_token_lock:
                token = bot_ref.spotify_key
                if token is None:
                    async with bot_ref.session.post(
                        "https://accounts.spotify.com/api/token",
                        data={"grant_type": "client_credentials"},
                        headers={
                            "Authorization": f"Basic {encoded}",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            raise HTTPException(502, "Spotify auth failed")
                        token_data = await resp.json()
                    token = token_data.get("access_token")
                    if not isinstance(token, str) or not token:
                        raise HTTPException(502, "Spotify auth returned no token")
                    bot_ref.spotify_key = token
        headers = {"Authorization": f"Bearer {token}"}
        q = f"track:{track} artist:{artist}"
        async with bot_ref.session.get(
            "https://api.spotify.com/v1/search",
            params={"q": q, "type": "track", "limit": 1},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise HTTPException(502, "Spotify search failed")
            data = await resp.json()
    items = data.get("tracks", {}).get("items", [])
    images = items[0].get("album", {}).get("images", []) if items else []
    cover_url = images[0].get("url") if images and isinstance(images[0], dict) else None
    if not isinstance(cover_url, str) or not cover_url:
        _spotify_cover_negative_cache[cache_key] = True
        raise HTTPException(404, "No cover found")
    _spotify_cover_cache[cache_key] = cover_url
    return {"url": cover_url}


class MessagePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=1, max_length=2000)
    avatar_url: str | None = Field(None, max_length=2048)
    discord_id: str | None = Field(None, pattern=r"^[0-9]{1,20}$")


_msg_rate_limit = TTLCache[str, bool](maxsize=10_000, ttl=60)


def _message_challenge_secret() -> bytes:
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    keys = bot_ref.config.get("keys", {})
    # Keep the challenge independent from the public media key when one is
    # configured, while allowing existing deployments to use their server
    # secret without a new environment variable.
    secret = (
        keys.get("message_challenge")
        or keys.get("message_challenge_secret")
        or _active_client_secret()
    )
    if not isinstance(secret, str) or not secret:
        raise HTTPException(503, "Message challenge is not configured")
    return secret.encode()


def _message_challenge(ip: str) -> str:
    issued = int(time.time())
    secret = _message_challenge_secret()
    payload = json.dumps(
        {
            "issued": issued,
            "nonce": secrets.token_urlsafe(24),
            "ip": hmac.new(secret, ip.encode(), hashlib.sha256).hexdigest(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _verify_message_challenge(
    supplied: str | None,
    cookie: str | None,
    ip: str,
) -> str:
    if not supplied or not cookie or not hmac.compare_digest(supplied, cookie):
        raise HTTPException(403, "Missing or invalid message challenge")
    try:
        encoded, signature = supplied.split(".", 1)
        secret = _message_challenge_secret()
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        issued = int(payload["issued"])
        ip_digest = str(payload["ip"])
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise HTTPException(403, "Missing or invalid message challenge")
    if abs(time.time() - issued) > MESSAGE_CHALLENGE_MAX_AGE:
        raise HTTPException(403, "Message challenge expired")
    expected_ip = hmac.new(secret, ip.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(ip_digest, expected_ip):
        raise HTTPException(403, "Message challenge does not match this client")
    token_hash = hashlib.sha256(supplied.encode()).hexdigest()
    if token_hash in _message_used_challenges:
        raise HTTPException(403, "Message challenge has already been used")
    return token_hash


async def _consume_message_challenge(token_hash: str, ip: str) -> None:
    now = time.monotonic()
    async with _message_rate_lock:
        if token_hash in _message_used_challenges:
            raise HTTPException(403, "Message challenge has already been used")
        if ip in _msg_rate_limit:
            raise HTTPException(
                429,
                "Please wait before sending another message",
                headers={"Retry-After": "60"},
            )
        while (
            _message_global_requests
            and now - _message_global_requests[0] >= MESSAGE_GLOBAL_RATE_WINDOW
        ):
            _message_global_requests.popleft()
        if len(_message_global_requests) >= MESSAGE_GLOBAL_RATE_LIMIT:
            retry_after = max(
                1,
                int(MESSAGE_GLOBAL_RATE_WINDOW - (now - _message_global_requests[0]))
                + 1,
            )
            raise HTTPException(
                429,
                "Message service is busy; please try again later",
                headers={"Retry-After": str(retry_after)},
            )
        _message_global_requests.append(now)
        _msg_rate_limit[ip] = True
        _message_used_challenges[token_hash] = True


def _sanitize_message_text(value: str) -> str:
    # Embeds normally do not ping, but explicitly neutralize every mention and
    # also send an empty allowed_mentions policy to the webhook as defense in
    # depth.
    return re.sub(
        r"@(everyone|here|&\d+|!?\d+)",
        lambda match: "@\u200b" + match.group(1),
        value,
        flags=re.IGNORECASE,
    )


def _client_ip(request: Request) -> str:
    remote = request.client.host if request.client else ""
    configured = os.environ.get("FISHIE_TRUSTED_PROXIES", "127.0.0.0/8,::1/128").split(
        ","
    )
    trusted_networks: list[ipaddress._BaseNetwork] = []
    for value in configured:
        value = value.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            # A malformed proxy entry must never expand trust; ignore it and
            # fall back to the socket address below.
            continue
        # A catch-all proxy range would make spoofed forwarding headers
        # authoritative for every client, defeating the rate limits.
        if network.prefixlen == 0:
            continue
        trusted_networks.append(network)
    try:
        remote_ip = ipaddress.ip_address(remote)
        trusted = any(remote_ip in network for network in trusted_networks)
    except ValueError:
        trusted = False

    candidate = remote
    if trusted:
        candidate = (
            (
                request.headers.get("CF-Connecting-IP")
                or request.headers.get("X-Real-IP")
                or remote
            )
            .split(",")[0]
            .strip()
        )
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError as error:
        raise HTTPException(400, "Invalid client address") from error


@app.get("/send-message/challenge")
async def send_message_challenge(response: Response, request: Request):
    """Issue a short-lived, one-time token for the public website form."""

    _require_website_origin(request)
    ip = _client_ip(request)
    token = _message_challenge(ip)
    response.set_cookie(
        key=MESSAGE_CHALLENGE_COOKIE,
        value=token,
        max_age=MESSAGE_CHALLENGE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "expires_in": MESSAGE_CHALLENGE_MAX_AGE}


@app.post("/send-message")
async def send_message(
    payload: MessagePayload,
    request: Request,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
    challenge: str | None = Header(None, alias="X-Fishie-Message-Challenge"),
    challenge_cookie: str | None = Cookie(None, alias=MESSAGE_CHALLENGE_COOKIE),
):
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")

    _require_website_origin(request)

    webhook_url = bot_ref.config["webhooks"].get("messages", "")
    if not webhook_url:
        raise HTTPException(500, "Webhook not configured")

    ip = _client_ip(request)

    if ip in bot_ref.cached_banned_ips:
        raise HTTPException(403, "You are banned from sending messages")

    challenge_hash = _verify_message_challenge(challenge, challenge_cookie, ip)
    await _consume_message_challenge(challenge_hash, ip)

    if payload.avatar_url:
        try:
            parsed_avatar = urlsplit(payload.avatar_url)
            hostname = (parsed_avatar.hostname or "").casefold().rstrip(".")
            if hostname not in DISCORD_ATTACHMENT_HOSTS:
                raise commands.BadArgument("Avatar URLs must use the Discord CDN")
            await validate_public_url(
                payload.avatar_url,
                allowed_hosts=DISCORD_ATTACHMENT_HOSTS,
                allow_http=False,
            )
        except (commands.CommandError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
    if payload.discord_id:
        user = await _verify_token(authorization, session_id)
        if not hmac.compare_digest(str(user["id"]), payload.discord_id):
            raise HTTPException(403, "The Discord identity does not match your session")

    # sanitize
    name = _sanitize_message_text(
        payload.name.replace("discord.com/api/webhooks", "[redacted]")
    )
    content = _sanitize_message_text(
        payload.content.replace("discord.com/api/webhooks", "[redacted]")
    )

    embed = {
        "author": (
            {"name": name, "icon_url": payload.avatar_url}
            if payload.avatar_url
            else {"name": name}
        ),
        "description": content,
        "footer": {
            "text": "Source: "
            + hmac.new(
                _active_client_secret().encode(),
                ip.encode(),
                hashlib.sha256,
            ).hexdigest()[:16]
        },
        "color": 0xFAA0C1,
    }
    if payload.discord_id:
        embed["footer"]["text"] += f" · ID: {payload.discord_id}"

    async with bot_ref.session.post(
        webhook_url,
        json={
            "embeds": [embed],
            "allowed_mentions": {"parse": []},
        },
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status not in (200, 204):
            raise HTTPException(502, "Message delivery failed")

    return {"ok": True}


@app.get("/ror2-items")
async def get_ror2_items():
    """Return all RoR2 items from the database."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    pool = bot_ref.pool
    if not pool:
        raise HTTPException(503, "Database not connected")
    rows = await pool.fetch("SELECT * FROM ror2_items ORDER BY name")
    items = []
    for r in rows:
        items.append(
            {
                "internal_name": r["internal_name"],
                "name": r["name"],
                "desc_short": r.get("desc_short", ""),
                "desc_full": r.get("desc_full", ""),
                "rarity": r.get("rarity", ""),
                "categories": r.get("categories", []),
                "achievement_locked": r.get("achievement_locked", ""),
                "stats": r.get("stats") or {},
                "lore": r.get("lore", ""),
                "corrupted_iname": r.get("corrupted_iname", ""),
                "extra": r.get("extra") or {},
            }
        )
    return {"items": items, "count": len(items)}


@app.get("/user/{user_id}/reminders")
async def get_user_reminders(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get reminders for a user. Requires OAuth."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own reminders")
    pool = _check_pool()
    rows = await pool.fetch(
        "SELECT id, expires, created, event, timezone, extra #>> '{args,2}' AS content "
        "FROM reminders WHERE event = 'reminder' AND extra #>> '{args,0}' = $1 ORDER BY expires",
        str(user_id),
    )
    return {
        "reminders": [
            {
                "id": r["id"],
                "expires": str(r["expires"]),
                "content": r["content"],
                "timezone": r["timezone"],
            }
            for r in rows
        ]
    }


@app.get("/user/{user_id}/first-command")
async def get_user_first_command(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get the date of a user's first command use."""
    await _require_self(user_id, authorization, session_id)
    pool = _check_pool()
    row = await pool.fetchrow(
        "SELECT created_at FROM command_logs WHERE user_id = $1 ORDER BY created_at ASC LIMIT 1",
        user_id,
    )
    return {"first_command": str(row["created_at"]) if row else None}


@app.get("/user/{user_id}/accounts")
async def get_user_accounts(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get connected accounts for a user. Requires OAuth."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own accounts")
    pool = _check_pool()
    row = await pool.fetchrow("SELECT * FROM accounts WHERE user_id = $1", user_id)
    if not row:
        return {"accounts": {}}
    accounts = {field: row[field] for field in LASTFM_ACCOUNT_FIELDS if row[field]}
    if row["steam"]:
        display_name = await _steam_display_name(row["steam"])
        if display_name:
            accounts["steam_display_name"] = display_name
    return {"accounts": accounts}


@app.post("/user/{user_id}/accounts")
async def set_user_accounts(
    user_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Set connected accounts. Requires OAuth."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    # Last.fm and Steam can only be changed through their authorization flows.
    allowed = {"roblox", "letterboxd"}
    accounts = {k: v for k, v in payload.get("accounts", {}).items() if k in allowed}
    pool = _check_pool()
    if accounts:
        keys = ", ".join(accounts.keys())
        vals = ", ".join(f"${i+2}" for i in range(len(accounts)))
        placeholders = list(accounts.values())
        await pool.execute(
            f"INSERT INTO accounts (user_id, {keys}) VALUES ($1, {vals}) ON CONFLICT (user_id) DO UPDATE SET {', '.join(f'{k}=EXCLUDED.{k}' for k in accounts)}",
            user_id,
            *placeholders,
        )
    return {"accounts": accounts}


@app.delete("/user/{user_id}/lastfm")
async def disconnect_lastfm(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Disconnect Last.fm for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    pool = _check_pool()
    await pool.execute(
        "UPDATE accounts SET lastfm = NULL, lastfm_session_key = NULL WHERE user_id = $1",
        user_id,
    )
    if bot_ref:
        await bot_ref.refresh_account_cache(user_id)
    return {"disconnected": True}


@app.delete("/user/{user_id}/steam")
async def disconnect_steam(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Disconnect Steam for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    pool = _check_pool()
    await pool.execute("UPDATE accounts SET steam = NULL WHERE user_id = $1", user_id)
    return {"disconnected": True}


@app.delete("/user/{user_id}/anilist")
async def disconnect_anilist(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Disconnect AniList for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    pool = _check_pool()
    await pool.execute(
        "UPDATE accounts SET anilist = NULL, anilist_access_token = NULL "
        "WHERE user_id = $1",
        user_id,
    )
    if bot_ref:
        await bot_ref.refresh_account_cache(user_id)
    return {"disconnected": True}


class GuildSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_download: int | str | None = None
    poketwo: StrictBool | None = None
    auto_reactions: StrictBool | None = None
    pinboard: int | str | None = None
    honeypot: int | str | None = None


@app.get("/guild/{guild_id}/settings")
async def get_guild_settings(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get guild settings. Requires OAuth."""
    me = await _verify_token(authorization, session_id)
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild = bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "Need Manage Server permission")
    pool = _check_pool()
    row = await pool.fetchrow(
        "SELECT * FROM guild_settings WHERE guild_id = $1", guild_id
    )
    hp = await pool.fetchrow(
        "SELECT channel_id FROM honeypot_channels WHERE guild_id = $1", guild_id
    )
    settings = {
        "auto_download": None,
        "poketwo": False,
        "auto_reactions": False,
        "pinboard": None,
        "honeypot": None,
    }
    if row:
        for k in ("auto_download", "poketwo", "auto_reactions", "pinboard"):
            settings[k] = row[k]
    if hp:
        settings["honeypot"] = hp["channel_id"]
    return settings


@app.post("/guild/{guild_id}/settings")
async def set_guild_settings(
    guild_id: int,
    payload: "GuildSettingsPayload",
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    me = await _verify_token(authorization, session_id)
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild = bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "Need Manage Server permission")
    payload_data = payload.model_dump(exclude_unset=True)
    updates = {}
    pool = _check_pool()

    current = await pool.fetchrow(
        "SELECT auto_download, poketwo, auto_reactions, pinboard "
        "FROM guild_settings WHERE guild_id = $1",
        guild_id,
    )
    if "honeypot" in payload_data:
        hp_val = payload_data["honeypot"]
        if hp_val:
            try:
                hp_val = int(hp_val)
            except (TypeError, ValueError):
                raise HTTPException(400, "honeypot must be a channel ID")
            channel = await _resolve_guild_text_channel(guild, hp_val)
            if channel is None:
                raise HTTPException(400, "Honeypot channel must belong to this server")
            if not (
                member.guild_permissions.manage_channels
                and member.guild_permissions.ban_members
            ):
                raise HTTPException(
                    403, "Manage Channels and Ban Members are required for honeypots"
                )
            me_member = guild.me
            if me_member is None or not (
                me_member.guild_permissions.manage_channels
                and me_member.guild_permissions.ban_members
            ):
                raise HTTPException(400, "The bot lacks channel or ban permissions")
        updates["honeypot"] = hp_val

    gs_updates = {}
    for key, value in payload_data.items():
        if key == "honeypot":
            continue
        if key in {"auto_download", "pinboard"}:
            if value in (None, ""):
                value = None
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be a channel ID")
            if value is not None:
                channel = await _resolve_guild_text_channel(guild, value)
                if channel is None:
                    raise HTTPException(
                        400, f"{key} channel must belong to this server"
                    )
                if not member.guild_permissions.manage_channels:
                    raise HTTPException(403, "Manage Channels is required")
        gs_updates[key] = value

    async with pool.acquire() as connection:
        async with connection.transaction():
            if "honeypot" in updates:
                if updates["honeypot"]:
                    await connection.execute(
                        "INSERT INTO honeypot_channels (guild_id, channel_id) VALUES ($1, $2) "
                        "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2",
                        guild_id,
                        updates["honeypot"],
                    )
                else:
                    await connection.execute(
                        "DELETE FROM honeypot_channels WHERE guild_id = $1", guild_id
                    )
            if gs_updates:
                keys = ", ".join(gs_updates.keys())
                placeholders = ", ".join(f"${i+2}" for i in range(len(gs_updates)))
                set_clause = ", ".join(f"{k} = EXCLUDED.{k}" for k in gs_updates)
                await connection.execute(
                    f"INSERT INTO guild_settings (guild_id, {keys}) VALUES ($1, {placeholders}) ON CONFLICT (guild_id) DO UPDATE SET {set_clause}",
                    guild_id,
                    *list(gs_updates.values()),
                )
                updates.update(gs_updates)

    # Keep the running bot in sync with dashboard changes.  These values are
    # read from db_cache by the event cogs and are otherwise stale until restart.
    cache = bot_ref.db_cache
    if "auto_download" in gs_updates:
        old = current["auto_download"] if current else None
        if old:
            cache.remove_adl(old)
        if gs_updates["auto_download"]:
            cache.add_adl(gs_updates["auto_download"])
    if "poketwo" in gs_updates:
        if gs_updates["poketwo"]:
            if guild_id not in cache.poketwo_guilds:
                cache.add_poketwo(guild_id)
        else:
            cache.remove_poketwo(guild_id)
    if "auto_reactions" in gs_updates:
        if gs_updates["auto_reactions"]:
            if guild_id not in cache.auto_reaction_guilds:
                cache.add_reaction_guilds(guild_id)
        else:
            cache.remove_reaction_guilds(guild_id)
    if "pinboard" in gs_updates:
        cache.pinboard.pop(guild_id, None)
        if gs_updates["pinboard"]:
            cache.add_pinboard(guild_id, gs_updates["pinboard"])
    if "honeypot" in updates:
        if updates["honeypot"]:
            bot_ref.cached_honeypots[guild_id] = updates["honeypot"]
        else:
            bot_ref.cached_honeypots.pop(guild_id, None)

    return {"settings": updates}


def _dashboard_command_list():
    if not bot_ref:
        return []
    bot = bot_ref
    commands_by_name = {}

    def add_command(command):
        if (
            command.hidden
            or command.cog_name in ("Owner", "Jishaku")
            or bot._command_disable_excluded(command)
        ):
            return
        name = command.qualified_name.casefold()
        commands_by_name[name] = {
            "name": command.qualified_name,
            "description": command.description or command.short_doc or "",
        }

    for command in bot_ref.commands:
        add_command(command)
    return sorted(commands_by_name.values(), key=lambda item: item["name"].casefold())


async def _managed_guild(
    guild_id: int, authorization: str | None, session_id: str | None
):
    me = await _verify_token(authorization, session_id)
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild = bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "Need Manage Server permission")
    return guild


@app.get("/guild/{guild_id}/command-disables")
async def get_command_disables(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    pool = _check_pool()
    rows = await pool.fetch(
        "SELECT command, channel_id FROM command_disables WHERE guild_id = $1",
        guild_id,
    )
    return {
        "commands": _dashboard_command_list(),
        "channels": [
            {"id": str(channel.id), "name": channel.name}
            for channel in guild.text_channels
        ],
        "disabled": [
            {"command": row["command"], "channel_id": int(row["channel_id"])}
            for row in rows
        ],
    }


@app.post("/guild/{guild_id}/command-disables")
async def set_command_disable(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")

    requested = str(payload.get("command", "")).strip()
    command = bot_ref.get_command(requested.casefold())
    if command is None or command.qualified_name.casefold() != requested.casefold():
        raise HTTPException(400, "Unknown command")
    if bot_ref._command_disable_excluded(command):
        raise HTTPException(400, "This command cannot be disabled")

    try:
        channel_id = int(payload.get("channel_id", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "channel_id must be a text channel ID or 0")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel is None or channel not in guild.text_channels:
            raise HTTPException(
                400, "channel_id must belong to a text channel in this server"
            )

    pool = _check_pool()
    command_name = command.qualified_name.casefold()
    if bool(payload.get("disabled")):
        await pool.execute(
            """
            INSERT INTO command_disables (guild_id, command, channel_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, command, channel_id) DO NOTHING
            """,
            guild_id,
            command_name,
            channel_id,
        )
        bot_ref.db_cache.add_disabled_command(guild_id, command_name, channel_id)
        disabled = True
    else:
        await pool.execute(
            "DELETE FROM command_disables WHERE guild_id = $1 AND command = $2 AND channel_id = $3",
            guild_id,
            command_name,
            channel_id,
        )
        bot_ref.db_cache.remove_disabled_command(guild_id, command_name, channel_id)
        disabled = False
    return {"command": command_name, "channel_id": channel_id, "disabled": disabled}


LOGGER_EVENT_LABELS = {
    "avatar": "Avatar changes",
    "member": "Member joins, leaves, and name changes",
    "activity": "Member game and activity changes",
    "voice": "Voice channel joins, leaves, moves, mutes, deafens, and disconnects",
    "channel": "Channel changes",
    "role": "Role changes",
    "server": "Server changes",
    "moderation": "Bans, kicks, unbans, and timeouts",
    "message": "Message edits, deletions, and purges",
}


async def _resolve_guild_text_channel(guild: Any, channel_id: int) -> Any | None:
    """Resolve a guild text channel even when it is not in the local cache."""
    channel = next(
        (item for item in guild.text_channels if item.id == channel_id),
        None,
    )
    if channel is not None:
        return channel
    if bot_ref is None:
        return None
    try:
        channel = await bot_ref.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    if (
        isinstance(channel, discord.TextChannel)
        and channel.guild is not None
        and channel.guild.id == guild.id
    ):
        return channel
    return None


@app.get("/guild/{guild_id}/twitch-follows")
async def get_twitch_follows(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    rows = await _check_pool().fetch(
        "SELECT channel_name, announce_channel_id, message_template, broadcaster_id "
        "FROM twitch_follows WHERE guild_id = $1 ORDER BY channel_name",
        guild_id,
    )
    channels = {str(channel.id): channel.name for channel in guild.text_channels}
    return {
        "follows": [
            {
                "channel_name": row["channel_name"],
                "announce_channel_id": int(row["announce_channel_id"]),
                "announce_channel_name": channels.get(
                    str(row["announce_channel_id"]), "Unknown channel"
                ),
                "message_template": row["message_template"],
                "broadcaster_id": row["broadcaster_id"],
            }
            for row in rows
        ],
        "channels": [
            {"id": str(channel.id), "name": channel.name}
            for channel in guild.text_channels
        ],
    }


@app.post("/guild/{guild_id}/twitch-follows")
async def set_twitch_follow(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    channel_name = str(payload.get("channel_name", "")).strip().lstrip("@").lower()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", channel_name):
        raise HTTPException(400, "Invalid Twitch channel name")
    raw_channel_id = payload.get("announce_channel_id")
    if not isinstance(raw_channel_id, (str, int)):
        raise HTTPException(400, "announce_channel_id must be a text channel ID")
    try:
        announce_channel_id = int(raw_channel_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "announce_channel_id must be a text channel ID")
    # Resolve from the guild's text-channel collection instead of relying only
    # on the cache-backed get_channel lookup.  A freshly loaded guild can have
    # valid channels that are not present in that cache yet.
    announce_channel = await _resolve_guild_text_channel(guild, announce_channel_id)
    if announce_channel is None:
        raise HTTPException(400, "Announcement channel must belong to this server")
    message_template = payload.get("message_template")
    if message_template is not None:
        message_template = str(message_template).strip() or None
        if message_template and len(message_template) > 2000:
            raise HTTPException(400, "The Twitch message cannot exceed 2000 characters")

    events: Any = bot_ref.get_cog("Events") if bot_ref else None
    if events is None or not hasattr(events, "_get_twitch_user"):
        raise HTTPException(503, "Twitch monitoring is unavailable")
    twitch_user = await events._get_twitch_user(channel_name)
    if not twitch_user or not twitch_user.get("id"):
        raise HTTPException(404, "Twitch channel not found")
    broadcaster_id = str(twitch_user["id"])
    pool = _check_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"fishie:twitch:{guild_id}",
            )
            existing = await connection.fetchval(
                "SELECT 1 FROM twitch_follows WHERE guild_id = $1 AND channel_name = $2",
                guild_id,
                channel_name,
            )
            if not existing:
                count = await connection.fetchval(
                    "SELECT COUNT(*) FROM twitch_follows WHERE guild_id = $1", guild_id
                )
                if count >= 3:
                    raise HTTPException(
                        400, "You can follow up to 3 Twitch channels per server"
                    )
            await connection.execute(
                """INSERT INTO twitch_follows
                   (guild_id, channel_name, announce_channel_id, broadcaster_id, message_template)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (guild_id, channel_name) DO UPDATE SET
                     announce_channel_id = EXCLUDED.announce_channel_id,
                     broadcaster_id = EXCLUDED.broadcaster_id,
                     message_template = EXCLUDED.message_template""",
                guild_id,
                channel_name,
                announce_channel_id,
                broadcaster_id,
                message_template,
            )
    try:
        await events.ensure_twitch_eventsub_subscription(broadcaster_id)
    except Exception as error:
        cast(Any, bot_ref).logger.warning(
            "Dashboard Twitch subscription failed: %s", error
        )
    return {"channel_name": channel_name}


@app.delete("/guild/{guild_id}/twitch-follows/{channel_name}")
async def delete_twitch_follow(
    guild_id: int,
    channel_name: str,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    await _managed_guild(guild_id, authorization, session_id)
    channel_name = channel_name.strip().lstrip("@").lower()
    pool = _check_pool()
    broadcaster_id = await pool.fetchval(
        "SELECT broadcaster_id FROM twitch_follows WHERE guild_id = $1 AND channel_name = $2",
        guild_id,
        channel_name,
    )
    result = await pool.execute(
        "DELETE FROM twitch_follows WHERE guild_id = $1 AND channel_name = $2",
        guild_id,
        channel_name,
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Twitch channel is not followed")
    events: Any = bot_ref.get_cog("Events") if bot_ref else None
    if broadcaster_id and events is not None:
        await events.remove_twitch_eventsub_subscription(str(broadcaster_id))
    return {"channel_name": channel_name}


@app.get("/guild/{guild_id}/youtube-follows")
async def get_youtube_follows(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    rows = await _check_pool().fetch(
        "SELECT youtube_channel_id, channel_name, channel_handle, "
        "announce_channel_id, message_template, event_types "
        "FROM youtube_follows WHERE guild_id = $1 ORDER BY channel_name",
        guild_id,
    )
    channels = {str(channel.id): channel.name for channel in guild.text_channels}
    return {
        "follows": [
            {
                "youtube_channel_id": row["youtube_channel_id"],
                "channel_name": row["channel_name"],
                "channel_handle": row["channel_handle"],
                "announce_channel_id": str(row["announce_channel_id"]),
                "announce_channel_name": channels.get(
                    str(row["announce_channel_id"]), "Unknown channel"
                ),
                "message_template": row["message_template"],
                "event_types": list(row["event_types"]),
            }
            for row in rows
        ],
        "channels": [
            {"id": str(channel.id), "name": channel.name}
            for channel in guild.text_channels
        ],
        "event_types": ["video", "live", "short", "community"],
    }


@app.post("/guild/{guild_id}/youtube-follows")
async def set_youtube_follow(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    raw_channel_id = payload.get("announce_channel_id")
    if not isinstance(raw_channel_id, (str, int)):
        raise HTTPException(400, "announce_channel_id must be a text channel ID")
    try:
        announce_channel_id = int(raw_channel_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "announce_channel_id must be a text channel ID")
    announce_channel = await _resolve_guild_text_channel(guild, announce_channel_id)
    if announce_channel is None:
        raise HTTPException(400, "Announcement channel must belong to this server")
    message_template = str(payload.get("message_template") or "").strip() or None
    if message_template and len(message_template) > 2000:
        raise HTTPException(400, "The YouTube message cannot exceed 2000 characters")
    event_types = normalize_youtube_events(payload.get("event_types"))
    if not event_types:
        raise HTTPException(
            400, "Choose at least one of: video, live, short, community"
        )
    events: Any = bot_ref.get_cog("Events") if bot_ref else None
    if events is None or not hasattr(events, "resolve_youtube_channel"):
        raise HTTPException(503, "YouTube notifications are unavailable")
    query = str(
        payload.get("youtube_channel_id") or payload.get("channel") or ""
    ).strip()
    youtube_channel = await events.resolve_youtube_channel(query)
    if youtube_channel is None:
        raise HTTPException(404, "YouTube channel not found")
    youtube_channel_id = str(youtube_channel["id"])
    pool = _check_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"fishie:youtube:{guild_id}",
            )
            existing = await connection.fetchval(
                "SELECT 1 FROM youtube_follows "
                "WHERE guild_id = $1 AND youtube_channel_id = $2",
                guild_id,
                youtube_channel_id,
            )
            if not existing:
                count = await connection.fetchval(
                    "SELECT COUNT(*) FROM youtube_follows WHERE guild_id = $1",
                    guild_id,
                )
                if count >= 3:
                    raise HTTPException(
                        400, "You can follow up to 3 YouTube channels per server"
                    )
            await connection.execute(
                """
                INSERT INTO youtube_follows
                    (guild_id, youtube_channel_id, channel_name, channel_handle,
                     announce_channel_id, message_template, event_types)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (guild_id, youtube_channel_id) DO UPDATE SET
                    channel_name = EXCLUDED.channel_name,
                    channel_handle = EXCLUDED.channel_handle,
                    announce_channel_id = EXCLUDED.announce_channel_id,
                    message_template = EXCLUDED.message_template,
                    event_types = EXCLUDED.event_types,
                    updated_at = now()
                """,
                guild_id,
                youtube_channel_id,
                youtube_channel["name"],
                youtube_channel.get("handle"),
                announce_channel_id,
                message_template,
                list(event_types),
            )
    await events.ensure_youtube_subscription(youtube_channel_id)
    return {
        "youtube_channel_id": youtube_channel_id,
        "channel_name": youtube_channel["name"],
    }


@app.delete("/guild/{guild_id}/youtube-follows/{youtube_channel_id}")
async def delete_youtube_follow(
    guild_id: int,
    youtube_channel_id: str,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    await _managed_guild(guild_id, authorization, session_id)
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", youtube_channel_id):
        raise HTTPException(400, "Invalid YouTube channel ID")
    result = await _check_pool().execute(
        "DELETE FROM youtube_follows "
        "WHERE guild_id = $1 AND youtube_channel_id = $2",
        guild_id,
        youtube_channel_id,
    )
    if result == "DELETE 0":
        raise HTTPException(404, "YouTube channel is not followed")
    events: Any = bot_ref.get_cog("Events") if bot_ref else None
    if events is not None and hasattr(events, "remove_youtube_subscription"):
        await events.remove_youtube_subscription(youtube_channel_id)
    return {"youtube_channel_id": youtube_channel_id}


@app.get("/guild/{guild_id}/logger")
async def get_logger_settings(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    rows = await _check_pool().fetch(
        "SELECT event, channel_id FROM guild_log_channels WHERE guild_id = $1",
        guild_id,
    )
    channels = {str(channel.id): channel.name for channel in guild.text_channels}
    return {
        "events": LOGGER_EVENT_LABELS,
        "channels": [
            {"id": str(channel.id), "name": channel.name}
            for channel in guild.text_channels
        ],
        "configured": [
            {
                "event": row["event"],
                "channel_id": int(row["channel_id"]),
                "channel_name": channels.get(str(row["channel_id"]), "Unknown channel"),
            }
            for row in rows
        ],
    }


@app.post("/guild/{guild_id}/logger")
async def set_logger_setting(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    event = str(payload.get("event", "")).strip().casefold()
    if event not in LOGGER_EVENT_LABELS:
        raise HTTPException(400, "Unknown logger event")
    raw_channel_id = payload.get("channel_id")
    if not isinstance(raw_channel_id, (str, int)):
        raise HTTPException(400, "channel_id must be a text channel ID")
    try:
        channel_id = int(raw_channel_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "channel_id must be a text channel ID")
    channel = await _resolve_guild_text_channel(guild, channel_id)
    if channel is None:
        raise HTTPException(400, "Logger channel must belong to this server")
    moderation: Any = bot_ref.get_cog("Moderation") if bot_ref else None
    if moderation is None or not hasattr(moderation, "_set_logger_channel"):
        raise HTTPException(503, "Logger is unavailable")
    actor_id = None
    try:
        actor_id = int((await _verify_token(authorization, session_id))["id"])
    except HTTPException:
        pass
    ctx = SimpleNamespace(guild=guild, author=SimpleNamespace(id=actor_id), bot=bot_ref)
    await moderation._set_logger_channel(
        ctx, event, channel, announce=False, actor_id=actor_id
    )
    return {"event": event, "channel_id": channel_id}


@app.delete("/guild/{guild_id}/logger/{event}")
async def delete_logger_setting(
    guild_id: int,
    event: str,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    await _managed_guild(guild_id, authorization, session_id)
    event = event.strip().casefold()
    if event not in LOGGER_EVENT_LABELS:
        raise HTTPException(400, "Unknown logger event")
    moderation: Any = bot_ref.get_cog("Moderation") if bot_ref else None
    pool = _check_pool()
    row = await pool.fetchrow(
        "SELECT webhook_url FROM guild_log_channels WHERE guild_id = $1 AND event = $2",
        guild_id,
        event,
    )
    if row and moderation is not None:
        await moderation._delete_logger_webhook(row["webhook_url"], guild_id, event)
    await pool.execute(
        "DELETE FROM guild_log_channels WHERE guild_id = $1 AND event = $2",
        guild_id,
        event,
    )
    return {"event": event}


@app.get("/guild/{guild_id}/prefixes")
async def get_guild_prefixes(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Get custom prefixes for a guild managed by the authenticated user."""
    await _managed_guild(guild_id, authorization, session_id)
    pool = _check_pool()
    rows = await pool.fetch(
        "SELECT prefix, author_id, time FROM guild_prefixes WHERE guild_id = $1 ORDER BY time",
        guild_id,
    )
    return {
        "prefixes": [
            {"prefix": r["prefix"], "author_id": r["author_id"], "time": str(r["time"])}
            for r in rows
        ]
    }


@app.post("/guild/{guild_id}/prefixes")
async def add_guild_prefix(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Add a custom prefix. Requires OAuth + Manage Server."""
    me, _, _ = await _require_guild_manager(guild_id, authorization, session_id)
    prefix = payload.get("prefix", "").strip()
    if not prefix or len(prefix) > 10:
        raise HTTPException(400, "Prefix must be 1-10 characters")
    pool = _check_pool()
    await pool.execute(
        "INSERT INTO guild_prefixes (guild_id, prefix, author_id, time) VALUES ($1, $2, $3, NOW()) ON CONFLICT (guild_id, prefix) DO UPDATE SET author_id = EXCLUDED.author_id, time = NOW()",
        guild_id,
        prefix,
        int(me["id"]),
    )
    if bot_ref:
        bot_ref.db_cache.add_prefix(guild_id, prefix)
    return {"prefix": prefix}


@app.delete("/guild/{guild_id}/prefixes")
async def remove_guild_prefix(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Remove a custom prefix. Requires OAuth + Manage Server."""
    await _require_guild_manager(guild_id, authorization, session_id)
    prefix = payload.get("prefix", "").strip()
    pool = _check_pool()
    await pool.execute(
        "DELETE FROM guild_prefixes WHERE guild_id = $1 AND prefix = $2",
        guild_id,
        prefix,
    )
    if bot_ref:
        bot_ref.db_cache.remove_prefix(guild_id, prefix)
    return {"prefix": prefix}


@app.delete("/guild/{guild_id}/data")
async def delete_guild_data(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Delete all tracking data for a guild. Requires OAuth + Manage Server."""
    await _require_guild_manager(guild_id, authorization, session_id)
    if bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    pool = _check_pool()
    logger_rows = await pool.fetch(
        "SELECT event, webhook_url FROM guild_log_channels WHERE guild_id = $1",
        guild_id,
    )
    broadcasters = await pool.fetch(
        "SELECT DISTINCT broadcaster_id FROM twitch_follows "
        "WHERE guild_id = $1 AND broadcaster_id IS NOT NULL",
        guild_id,
    )
    youtube_channels = await pool.fetch(
        "SELECT DISTINCT youtube_channel_id FROM youtube_follows "
        "WHERE guild_id = $1",
        guild_id,
    )
    auto_download_channel = await pool.fetchval(
        "SELECT auto_download FROM guild_settings WHERE guild_id = $1", guild_id
    )
    async with pool.acquire() as connection:
        async with connection.transaction():
            deleted = await erase_guild(connection, guild_id)

    moderation: Any = bot_ref.get_cog("Moderation")
    if moderation is not None:
        for row in logger_rows:
            await moderation._delete_logger_webhook(
                row["webhook_url"], guild_id, row["event"]
            )
    events: Any = bot_ref.get_cog("Events")
    if events is not None:
        for row in broadcasters:
            await events.remove_twitch_eventsub_subscription(str(row["broadcaster_id"]))
        for row in youtube_channels:
            await events.remove_youtube_subscription(str(row["youtube_channel_id"]))

    cache = bot_ref.db_cache
    cache.prefixes.pop(guild_id, None)
    cache.opted_out.pop(guild_id, None)
    cache.pinboard.pop(guild_id, None)
    cache.poketwo_guilds.discard(guild_id)
    cache.auto_reaction_guilds.discard(guild_id)
    if auto_download_channel:
        cache.auto_downloads.discard(int(auto_download_channel))
    cache.disabled_commands = {
        item for item in cache.disabled_commands if item[0] != guild_id
    }
    bot_ref.cached_honeypots.pop(guild_id, None)
    return {"deleted": True, "deleted_rows": deleted}
