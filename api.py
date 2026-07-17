"""
Fishie bot API | commands, stats, OAuth, and user data history.
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
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
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core import Fishie

WEB_ORIGINS = frozenset({"https://crygup.com", "https://www.crygup.com"})
SESSION_COOKIE = "__Host-fishie_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60

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
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.cookies.get(
        SESSION_COOKIE
    ):
        if request.headers.get("origin") not in WEB_ORIGINS:
            return JSONResponse(
                status_code=403, content={"detail": "Invalid request origin"}
            )
    return await call_next(request)


bot_ref: "Fishie | None" = None
TABLE_MAP = {
    "avatars": "avatars",
    "username_logs": "username_logs",
    "display_name_logs": "display_name_logs",
    "discrim_logs": "discrim_logs",
    "nickname_logs": "nickname_logs",
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
ANILIST_CALLBACK_URL = "https://crygup.com/fishie"
ANILIST_TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
ANILIST_STATE_TTL = 10 * 60
TWITCH_EVENTSUB_MAX_AGE = 10 * 60


def _lastfm_state(user_id: int, source: str) -> str:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    payload = json.dumps(
        {
            "user_id": str(user_id),
            "source": source,
            "expires": int(time.time()) + LASTFM_STATE_TTL,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        bot_ref.config["keys"]["lastfm_secret"].encode(),
        encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _lastfm_authorization_url(user_id: int, source: str) -> str:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    state = _lastfm_state(user_id, source)
    callback = f"{LASTFM_CALLBACK_URL}?{urlencode({'lastfm_state': state})}"
    return "https://www.last.fm/api/auth/?" + urlencode(
        {"api_key": bot_ref.config["keys"]["lastfm_cb"], "cb": callback}
    )


def _steam_state(
    user_id: int,
    source: str,
    channel_id: int | None = None,
    message_id: int | None = None,
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
    if channel_id is not None and message_id is not None:
        states[token]["channel_id"] = int(channel_id)
        states[token]["message_id"] = int(message_id)
    return token


def _steam_authorization_url(user_id: int, source: str) -> str:
    state = _steam_state(user_id, source)
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


def _decode_steam_state(state: str) -> tuple[int, str, int | None, int | None]:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    states = getattr(bot_ref, "_steam_oauth_states", None) or {}
    payload = states.pop(state, None)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Steam connection state")
    try:
        user_id = int(payload["user_id"])
        source = payload["source"]
        expires = int(payload["expires"])
        channel_id = int(payload["channel_id"]) if payload.get("channel_id") else None
        message_id = int(payload["message_id"]) if payload.get("message_id") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Invalid Steam connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid Steam connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The Steam connection link has expired")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
    return user_id, source, channel_id, message_id


def _decode_spotify_state(state: str) -> tuple[int, str, int | None, int | None]:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    if not state.startswith("spotify_"):
        raise HTTPException(400, "Invalid Spotify connection state")
    states = getattr(bot_ref, "_spotify_oauth_states", None) or {}
    payload = states.pop(state.removeprefix("spotify_"), None)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Spotify connection state")
    try:
        user_id = int(payload["user_id"])
        source = payload["source"]
        expires = int(payload["expires"])
        channel_id = int(payload["channel_id"]) if payload.get("channel_id") else None
        message_id = int(payload["message_id"]) if payload.get("message_id") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Invalid Spotify connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid Spotify connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The Spotify connection link has expired")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
    return user_id, source, channel_id, message_id


def _anilist_state(
    user_id: int,
    source: str,
    channel_id: int | None = None,
    message_id: int | None = None,
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
    if channel_id is not None and message_id is not None:
        states[token]["channel_id"] = int(channel_id)
        states[token]["message_id"] = int(message_id)
    return token


def _anilist_authorization_url(user_id: int, source: str) -> str:
    state = _anilist_state(user_id, source)
    return "https://anilist.co/api/v2/oauth/authorize?" + urlencode(
        {
            "client_id": bot_ref.config["keys"]["anilist_id"],
            "redirect_uri": ANILIST_CALLBACK_URL,
            "response_type": "code",
            "state": f"anilist_{state}",
        }
    )


def _decode_anilist_state(state: str) -> tuple[int, str, int | None, int | None]:
    if not bot_ref or not state.startswith("anilist_"):
        raise HTTPException(400, "Invalid AniList connection state")
    states = getattr(bot_ref, "_anilist_oauth_states", None) or {}
    payload = states.pop(state.removeprefix("anilist_"), None)
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid AniList connection state")
    try:
        user_id = int(payload["user_id"])
        source = payload["source"]
        expires = int(payload["expires"])
        channel_id = int(payload["channel_id"]) if payload.get("channel_id") else None
        message_id = int(payload["message_id"]) if payload.get("message_id") else None
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Invalid AniList connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid AniList connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The AniList connection link has expired")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
    return user_id, source, channel_id, message_id


def _decode_lastfm_state(
    state: str,
) -> tuple[int, str, int | None, int | None]:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
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
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(400, "Invalid Last.fm connection state")
    if source not in {"discord", "website"}:
        raise HTTPException(400, "Invalid Last.fm connection source")
    if expires < int(time.time()):
        raise HTTPException(400, "The Last.fm connection link has expired")
    if (channel_id is None) != (message_id is None):
        raise HTTPException(400, "Invalid Discord message state")
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

        channel = bot_ref.get_channel(channel_id)
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
                ctx,
                row=row,
                lastfm_connected=bool(row and row["lastfm"]),
                steam_connected=bool(row and row["steam"]),
                anilist_connected=bool(row and row["anilist"]),
            ),
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
    tasks = getattr(bot_ref, "_oauth_refresh_tasks", None)
    if tasks is None:
        tasks = bot_ref._oauth_refresh_tasks = set()
    task = asyncio.create_task(
        _refresh_discord_accounts_message(user_id, channel_id, message_id)
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def init(bot: "Fishie") -> None:
    global bot_ref
    bot_ref = bot


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
        discord_access_token,
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
    return row["discord_access_token"]


def _verify_twitch_eventsub(request: Request, body: bytes) -> None:
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    keys = bot_ref.config["keys"]
    secret = keys.get("twitch_eventsub_secret") or keys.get("twitch_secret")
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

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid Twitch EventSub payload")

    message_type = request.headers.get("Twitch-Eventsub-Message-Type")
    bot_ref.logger.info(
        "Received Twitch EventSub message type=%s", message_type or "unknown"
    )
    if message_type == "webhook_callback_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(400, "Missing Twitch EventSub challenge")
        subscription = payload.get("subscription")
        if isinstance(subscription, dict) and subscription.get("id"):
            await bot_ref.pool.execute(
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
    message_id = request.headers.get("Twitch-Eventsub-Message-Id")
    if not isinstance(event, dict) or not message_id:
        raise HTTPException(400, "Missing Twitch EventSub event")
    event = dict(event)
    event["type"] = subscription.get("type")

    events_cog = bot_ref.get_cog("Events") if bot_ref else None
    if events_cog is None or not hasattr(events_cog, "handle_twitch_event"):
        raise HTTPException(503, "Twitch event handler is not ready")

    pool = _check_pool()
    result = await pool.execute(
        "INSERT INTO twitch_eventsub_events (message_id) VALUES ($1) "
        "ON CONFLICT (message_id) DO NOTHING",
        message_id,
    )
    if result == "INSERT 0 0":
        return {"ok": True, "duplicate": True}

    async def process_event():
        try:
            await events_cog.handle_twitch_event(event)
        except Exception as error:
            try:
                await pool.execute(
                    "DELETE FROM twitch_eventsub_events WHERE message_id = $1",
                    message_id,
                )
            except Exception as cleanup_error:
                bot_ref.logger.warning(
                    "Could not requeue failed Twitch EventSub event %s: %s",
                    message_id,
                    cleanup_error,
                )
            bot_ref.logger.warning("Twitch EventSub event processing failed: %s", error)

    asyncio.create_task(process_event())
    return {"ok": True}


async def _check_opted_out(user_id: int) -> bool:
    pool = _check_pool()
    r = await pool.fetchval(
        "SELECT 1 FROM opted_out WHERE user_id = $1 AND cardinality(items) > 0", user_id
    )
    return r is not None


VALID_OPTOUTS = {"avatar", "username", "display", "nickname", "discrim", "joins"}


@app.get("/user/{user_id}/opted-out")
async def get_opted_out(user_id: int):
    """Get the list of tracking methods this user has opted out of."""
    pool = _check_pool()
    row = await pool.fetchrow("SELECT items FROM opted_out WHERE user_id = $1", user_id)
    items = row["items"] if row else []
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
    pool = _check_pool()
    await pool.execute(
        "INSERT INTO opted_out (user_id, items) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET items = $2",
        user_id,
        items,
    )

    if bot_ref:
        if items:
            bot_ref.db_cache.opted_out[user_id] = items
        else:
            bot_ref.db_cache.opted_out.pop(user_id, None)

    return {"items": items}


async def _verify_token(
    authorization: str | None = None, session_id: str | None = None
) -> dict:
    import aiohttp

    if session_id:
        token = await _session_access_token(session_id)
    elif authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        raise HTTPException(401, "Missing access token")
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            "https://discord.com/api/users/@me", headers=headers
        ) as resp:
            if resp.status != 200:
                raise HTTPException(401, "Invalid access token")
            return await resp.json()


VALID_GUILD_OPTOUTS = {"name", "icon"}


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
    guilds = []
    for guild in bot_ref.guilds:
        member = guild.get_member(user_id)
        if member and member.guild_permissions.manage_guild:
            row = await pool.fetchrow(
                "SELECT items FROM guild_opted_out WHERE guild_id = $1", guild.id
            )
            guilds.append(
                {
                    "id": str(guild.id),
                    "name": guild.name,
                    "icon": str(guild.icon) if guild.icon else None,
                    "opted_out": row["items"] if row else [],
                }
            )

    guilds.sort(key=lambda g: g["name"].lower())
    return {"guilds": guilds}


@app.get("/guild/{guild_id}/opted-out")
async def get_guild_opted_out(guild_id: int):
    """Get opted-out tracking items for a guild."""
    pool = _check_pool()
    row = await pool.fetchrow(
        "SELECT items FROM guild_opted_out WHERE guild_id = $1", guild_id
    )
    return {"items": row["items"] if row else []}


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
    pool = _check_pool()
    await pool.execute(
        "INSERT INTO guild_opted_out (guild_id, items) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET items = $2",
        guild_id,
        items,
    )

    if bot_ref:
        if items:
            bot_ref.db_cache.opted_out[guild_id] = items
        else:
            bot_ref.db_cache.opted_out.pop(guild_id, None)

    return {"items": items}


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
    guild_id: int, page: int = Query(1, ge=1), per_page: int = Query(80, ge=1, le=100)
):
    """Get guild icon history."""
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
    url_map = dict(zip(urls, refreshed))
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
    guild_id: int, page: int = Query(1, ge=1), per_page: int = Query(80, ge=1, le=100)
):
    """Get guild name history."""
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

    def add_cmd(c):
        if c.hidden or c.cog_name in ("Owner", "Jishaku"):
            return
        aliases = ", ".join(c.aliases) if c.aliases else ""
        params = []
        for name, param in c.clean_params.items():
            req = "required" if param.default is param.empty else "optional"
            params.append({"name": name, "required": req})
        cmds.append(
            {
                "name": c.qualified_name,
                "description": c.description or c.short_doc or "",
                "category": c.cog_name or "Uncategorized",
                "usage": c.usage or "",
                "aliases": aliases,
                "params": params,
            }
        )

    for cmd in bot_ref.commands:
        add_cmd(cmd)
        if hasattr(cmd, "walk_commands"):
            for sub in cmd.walk_commands():
                add_cmd(sub)
    return {"commands": sorted(cmds, key=lambda c: (c["category"], c["name"]))}


@app.get("/stats")
async def bot_stats():
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    import datetime

    pool = _check_pool()
    async with pool.acquire() as conn:
        start = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        avatars_total = await conn.fetchval("SELECT COUNT(*) FROM avatars")
        avatars_today = await conn.fetchval(
            "SELECT COUNT(*) FROM avatars WHERE created_at >= $1", start
        )
        commands_total = await conn.fetchval("SELECT COUNT(*) FROM command_logs")
        commands_today = await conn.fetchval(
            "SELECT COUNT(*) FROM command_logs WHERE created_at >= $1", start
        )
        usernames_total = await conn.fetchval("SELECT COUNT(*) FROM username_logs")
        usernames_today = await conn.fetchval(
            "SELECT COUNT(*) FROM username_logs WHERE created_at >= $1", start
        )
        discrims_total = await conn.fetchval("SELECT COUNT(*) FROM discrim_logs")
        discrims_today = await conn.fetchval(
            "SELECT COUNT(*) FROM discrim_logs WHERE created_at >= $1", start
        )
        nicknames_total = await conn.fetchval("SELECT COUNT(*) FROM nickname_logs")
        nicknames_today = await conn.fetchval(
            "SELECT COUNT(*) FROM nickname_logs WHERE created_at >= $1", start
        )
        guild_names_total = await conn.fetchval("SELECT COUNT(*) FROM guild_name_logs")
        guild_names_today = await conn.fetchval(
            "SELECT COUNT(*) FROM guild_name_logs WHERE created_at >= $1", start
        )
        member_joins_total = await conn.fetchval(
            "SELECT COUNT(*) FROM member_join_logs"
        )
        member_joins_today = await conn.fetchval(
            "SELECT COUNT(*) FROM member_join_logs WHERE time >= $1", start
        )
        guild_icons_total = await conn.fetchval("SELECT COUNT(*) FROM guild_icons")
        guild_icons_today = await conn.fetchval(
            "SELECT COUNT(*) FROM guild_icons WHERE created_at >= $1", start
        )
        guild_avatars_total = await conn.fetchval("SELECT COUNT(*) FROM guild_avatars")
        guild_avatars_today = await conn.fetchval(
            "SELECT COUNT(*) FROM guild_avatars WHERE created_at >= $1", start
        )
    return {
        "guilds": len(bot_ref.guilds),
        "users": sum(g.member_count or 0 for g in bot_ref.guilds),
        "commands": len(bot_ref.commands),
        "uptime_seconds": (
            (datetime.datetime.now().astimezone() - bot_ref.start_time).total_seconds()
            if hasattr(bot_ref, "start_time")
            else 0
        ),
        "today": {
            "avatars": avatars_today,
            "commands": commands_today,
            "usernames": usernames_today,
            "discrims": discrims_today,
            "nicknames": nicknames_today,
            "guild_names": guild_names_today,
            "member_joins": member_joins_today,
            "guild_icons": guild_icons_today,
            "guild_avatars": guild_avatars_today,
        },
        "totals": {
            "avatars": avatars_total,
            "commands": commands_total,
            "usernames": usernames_total,
            "discrims": discrims_total,
            "nicknames": nicknames_total,
            "guild_names": guild_names_total,
            "member_joins": member_joins_total,
            "guild_icons": guild_icons_total,
            "guild_avatars": guild_avatars_total,
        },
    }


@app.get("/oauth/exchange")
@app.post("/oauth/exchange")
async def oauth_exchange(
    response: Response,
    code: str = Query(...),
    redirect_uri: str = Query("https://crygup.com/dashboard"),
):
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    if redirect_uri not in {"https://crygup.com", "https://crygup.com/dashboard"}:
        raise HTTPException(400, "Invalid OAuth redirect URI")
    data = {
        "client_id": str(bot_ref.config["ids"]["bot_id"]),
        "client_secret": bot_ref.config["keys"]["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://discord.com/api/oauth2/token", data=data
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise HTTPException(400, f"OAuth exchange failed: {err}")
            token_data = await resp.json()
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        async with session.get(
            "https://discord.com/api/users/@me", headers=headers
        ) as resp:
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
    if not session_id and not (
        authorization and authorization.startswith("Bearer ")
    ):
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
    response.headers["Cache-Control"] = "private, no-store"
    return {"url": _lastfm_authorization_url(int(me["id"]), "website")}


@app.get("/lastfm/callback")
async def lastfm_callback(token: str = Query(...), state: str = Query(...)):
    """Exchange a Last.fm callback token and persist the verified account."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise HTTPException(400, "Invalid Last.fm authentication token")

    user_id, source, channel_id, message_id = _decode_lastfm_state(state)
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
        session_key,
    )
    bot_ref.db_cache.add_account(user_id, username)
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    return {"username": username, "source": source}


@app.get("/steam/connect")
async def steam_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Create a Steam OpenID URL for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    response.headers["Cache-Control"] = "private, no-store"
    return {"url": _steam_authorization_url(int(me["id"]), "website")}


@app.get("/steam/callback")
async def steam_callback(request: Request, steam_state: str = Query(...)):
    """Verify a Steam OpenID response and persist the user's SteamID64."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_steam_state(steam_state)
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
    return {"steamid": steam_id, "personaname": persona_name, "source": source}


@app.get("/spotify/callback")
async def spotify_callback(code: str = Query(...), state: str = Query(...)):
    """Exchange a Spotify authorization code and persist the user's account."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_spotify_state(state)
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
        ) as response:
            try:
                token_data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                raise HTTPException(400, "Spotify returned an invalid token response")
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        bot_ref.logger.warning("Spotify token exchange unavailable: %s", error)
        raise HTTPException(400, "Spotify authorization service unavailable") from error
    bot_ref.logger.info(
        "Spotify OAuth token exchange completed status=%s user_id=%s",
        response.status,
        user_id,
    )
    if response.status != 200 or not isinstance(token_data, dict):
        error_code = token_data.get("error") if isinstance(token_data, dict) else None
        bot_ref.logger.warning(
            "Spotify OAuth token exchange rejected status=%s error=%s user_id=%s",
            response.status,
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
            refresh_token = await asyncio.wait_for(
                pool.fetchval(
                    "SELECT spotify_refresh_token FROM accounts WHERE user_id = $1",
                    user_id,
                ),
                timeout=5,
            )
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
                refresh_token,
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
                refresh_token,
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
    return {"display_name": display_name, "source": source}


@app.get("/anilist/connect")
async def anilist_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Create an AniList authorization URL for the authenticated Discord user."""
    me = await _verify_token(authorization, session_id)
    response.headers["Cache-Control"] = "private, no-store"
    return {"url": _anilist_authorization_url(int(me["id"]), "website")}


@app.get("/anilist/callback")
async def anilist_callback(code: str = Query(...), state: str = Query(...)):
    """Exchange an AniList code and persist the verified profile credentials."""
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_anilist_state(state)
    payload = {
        "grant_type": "authorization_code",
        "client_id": bot_ref.config["keys"]["anilist_id"],
        "client_secret": bot_ref.config["keys"]["anilist_secret"],
        "redirect_uri": ANILIST_CALLBACK_URL,
        "code": code,
    }
    async with bot_ref.session.post(ANILIST_TOKEN_URL, json=payload) as response:
        try:
            token_data = await response.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError):
            raise HTTPException(502, "AniList returned an invalid token response")
    access_token = (
        token_data.get("access_token") if isinstance(token_data, dict) else None
    )
    if response.status != 200 or not access_token:
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
        access_token,
    )
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    return {"username": username, "source": source}


@app.get("/user/{user_id}")
async def get_user_data(user_id: int):
    pool = _check_pool()
    async with pool.acquire() as conn:
        return {
            "user_id": user_id,
            "counts": {
                "avatars": await conn.fetchval(
                    "SELECT COUNT(*) FROM avatars WHERE user_id = $1", user_id
                ),
                "usernames": await conn.fetchval(
                    "SELECT COUNT(*) FROM username_logs WHERE user_id = $1", user_id
                ),
                "display_names": await conn.fetchval(
                    "SELECT COUNT(*) FROM display_name_logs WHERE user_id = $1", user_id
                ),
                "discrims": await conn.fetchval(
                    "SELECT COUNT(*) FROM discrim_logs WHERE user_id = $1", user_id
                ),
            },
        }


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
    user_id: int, page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=100)
):
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
    user_id: int, page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=100)
):
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
    user_id: int, page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=100)
):
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


@app.delete("/user/{user_id}")
async def delete_user_data(
    user_id: int,
    table: str = Query(None),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    me = await _verify_token(authorization, session_id)

    tables = [table] if table else list(TABLE_MAP.keys())
    deleted = 0
    pool = _check_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for t in tables:
                db_table = TABLE_MAP.get(t)
                if not db_table:
                    raise HTTPException(400, f"Invalid table: {t}")
                if t in GUILD_TABLES:
                    if not bot_ref:
                        raise HTTPException(503, "Bot not ready")
                    guild = bot_ref.get_guild(user_id)
                    if not guild:
                        raise HTTPException(404, "Guild not found")
                    member = guild.get_member(int(me["id"]))
                    if not member or not member.guild_permissions.manage_guild:
                        raise HTTPException(403, "You need Manage Server in this guild")
                    r = await conn.execute(
                        f"DELETE FROM {db_table} WHERE guild_id = $1", user_id
                    )
                else:
                    if int(me["id"]) != user_id:
                        raise HTTPException(403, "You can only delete your own data")
                    r = await conn.execute(
                        f"DELETE FROM {db_table} WHERE user_id = $1", user_id
                    )
                deleted += int(r.split()[-1])
    return {"user_id": user_id, "deleted_rows": deleted}


@app.get("/resolve")
async def resolve_user(q: str = Query(...)):
    """Resolve a Discord username or ID to a user ID."""
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
                r = await conn.execute(
                    f"DELETE FROM {db_table} WHERE guild_id = $1 AND icon_key = $2",
                    user_id,
                    key,
                )
            else:
                r = await conn.execute(
                    f"DELETE FROM {db_table} WHERE guild_id = $1 AND id = $2",
                    user_id,
                    int(key),
                )
        elif table == "avatars":
            if int(me["id"]) != user_id:
                raise HTTPException(403, "You can only delete your own data")
            r = await conn.execute(
                f"DELETE FROM {db_table} WHERE user_id = $1 AND avatar_key = $2",
                user_id,
                key,
            )
        else:
            if int(me["id"]) != user_id:
                raise HTTPException(403, "You can only delete your own data")
            r = await conn.execute(
                f"DELETE FROM {db_table} WHERE user_id = $1 AND id = $2",
                user_id,
                int(key),
            )
    return {"deleted": True}


@app.get("/spotify-cover")
async def spotify_cover(artist: str = Query(...), track: str = Query(...)):
    """Search Spotify for a track cover image. Falls back if Last.fm has no cover."""
    import base64

    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    sid = bot_ref.config["keys"]["spotify_id"]
    ss = bot_ref.config["keys"]["spotify_secret"]
    encoded = base64.b64encode(f"{sid}:{ss}".encode("ascii")).decode("ascii")
    async with aiohttp.ClientSession() as session:
        # Get token
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as resp:
            if resp.status != 200:
                raise HTTPException(502, "Spotify auth failed")
            token_data = await resp.json()
        # Search
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        q = f"track:{track} artist:{artist}"
        async with session.get(
            "https://api.spotify.com/v1/search",
            params={"q": q, "type": "track", "limit": 1},
            headers=headers,
        ) as resp:
            if resp.status != 200:
                raise HTTPException(502, "Spotify search failed")
            data = await resp.json()
    items = data.get("tracks", {}).get("items", [])
    if not items:
        raise HTTPException(404, "No cover found")
    images = items[0].get("album", {}).get("images", [])
    if not images:
        raise HTTPException(404, "No cover found")
    return {"url": images[0]["url"]}


class MessagePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=1, max_length=2000)
    avatar_url: str | None = None
    discord_id: str | None = None


_msg_rate_limit: dict[str, float] = {}


@app.post("/send-message")
async def send_message(payload: MessagePayload, request: Request):
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")

    webhook_url = bot_ref.config["webhooks"].get("messages", "")
    if not webhook_url:
        raise HTTPException(500, "Webhook not configured")

    # rate limit: 1 per minute per IP
    ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Real-IP")
        or (request.client.host if request.client else "unknown")
    )
    ip = ip.split(",")[0].strip()

    if ip in bot_ref.cached_banned_ips:
        raise HTTPException(403, "You are banned from sending messages")

    now = time.time()
    last = _msg_rate_limit.get(ip, 0)
    if now - last < 60:
        raise HTTPException(429, "Please wait before sending another message")
    _msg_rate_limit[ip] = now

    # sanitize
    name = payload.name.replace("discord.com/api/webhooks", "[redacted]")
    content = payload.content.replace("discord.com/api/webhooks", "[redacted]")

    embed = {
        "author": (
            {"name": name, "icon_url": payload.avatar_url}
            if payload.avatar_url
            else {"name": name}
        ),
        "description": content,
        "footer": {"text": f"IP: {ip}"},
        "color": 0xFAA0C1,
    }
    if payload.discord_id:
        embed["footer"]["text"] += f" · ID: {payload.discord_id}"

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, json={"embeds": [embed]}) as resp:
            if resp.status not in (200, 204):
                raise HTTPException(500, f"Webhook returned {resp.status}")

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
async def get_user_first_command(user_id: int):
    """Get the date of a user's first command use."""
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
        bot_ref.db_cache.lastfm.pop(user_id, None)
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
    return {"disconnected": True}


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
    payload: dict = Body(...),
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
    allowed = {"auto_download", "poketwo", "auto_reactions", "pinboard", "honeypot"}
    updates = {}
    pool = _check_pool()

    current = await pool.fetchrow(
        "SELECT auto_download, poketwo, auto_reactions, pinboard "
        "FROM guild_settings WHERE guild_id = $1",
        guild_id,
    )
    current_honeypot = await pool.fetchval(
        "SELECT channel_id FROM honeypot_channels WHERE guild_id = $1", guild_id
    )

    if "honeypot" in payload:
        hp_val = payload["honeypot"]
        if hp_val:
            try:
                hp_val = int(hp_val)
            except (TypeError, ValueError):
                raise HTTPException(400, "honeypot must be a channel ID")
            await pool.execute(
                "INSERT INTO honeypot_channels (guild_id, channel_id) VALUES ($1, $2) "
                "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2",
                guild_id,
                hp_val,
            )
        else:
            await pool.execute(
                "DELETE FROM honeypot_channels WHERE guild_id = $1", guild_id
            )
        updates["honeypot"] = hp_val

    gs_updates = {}
    for key, value in payload.items():
        if key not in allowed or key == "honeypot":
            continue
        if key in {"auto_download", "pinboard"}:
            if value in (None, ""):
                value = None
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be a channel ID")
        elif key in {"poketwo", "auto_reactions"}:
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "on"}
            else:
                value = bool(value)
        gs_updates[key] = value

    if gs_updates:
        keys = ", ".join(gs_updates.keys())
        placeholders = ", ".join(f"${i+2}" for i in range(len(gs_updates)))
        set_clause = ", ".join(f"{k} = EXCLUDED.{k}" for k in gs_updates)
        await pool.execute(
            f"INSERT INTO guild_settings (guild_id, {keys}) VALUES ($1, {placeholders}) ON CONFLICT (guild_id) DO UPDATE SET {set_clause}",
            guild_id,
            *list(gs_updates.values()),
        )
        for k, v in gs_updates.items():
            updates[k] = v

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
        if current_honeypot:
            bot_ref.cached_honeypots.discard(current_honeypot)
        if updates["honeypot"]:
            bot_ref.cached_honeypots.add(updates["honeypot"])

    return {"settings": updates}


@app.get("/guild/{guild_id}/prefixes")
async def get_guild_prefixes(guild_id: int):
    """Get custom prefixes for a guild."""
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
    try:
        me = await _verify_token(authorization, session_id)
        if not bot_ref:
            raise HTTPException(503, "Bot not ready")
        guild = bot_ref.get_guild(guild_id)
        if not guild:
            raise HTTPException(404, "Guild not found")
        member = guild.get_member(int(me["id"]))
        if not member or not member.guild_permissions.manage_guild:
            raise HTTPException(403, "Need Manage Server permission")
        prefix = payload.get("prefix", "").strip()
        if not prefix or len(prefix) > 10:
            raise HTTPException(400, "Prefix must be 1-10 characters")
        pool = _check_pool()
        await pool.execute(
            "INSERT INTO guild_prefixes (guild_id, prefix, author_id, time) VALUES ($1, $2, $3, NOW()) ON CONFLICT (guild_id, prefix) DO UPDATE SET author_id = EXCLUDED.author_id, time = NOW()",
            guild_id,
            prefix,
            int(payload.get("author_id", me["id"])),
        )
        if bot_ref:
            bot_ref.db_cache.add_prefix(guild_id, prefix)
        return {"prefix": prefix}
    except HTTPException:
        raise
    except Exception as e:
        import traceback

        print(f"[API ERROR] add_guild_prefix: {e}", flush=True)
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.delete("/guild/{guild_id}/prefixes")
async def remove_guild_prefix(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Remove a custom prefix. Requires OAuth + Manage Server."""
    me = await _verify_token(authorization, session_id)
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild = bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "Need Manage Server permission")
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
    for table in ("guild_icons", "guild_name_logs", "guild_avatars"):
        await pool.execute(f"DELETE FROM {table} WHERE guild_id = $1", guild_id)
    for table in (
        "guild_settings",
        "honeypot_channels",
        "guild_prefixes",
        "guild_opted_out",
    ):
        await pool.execute(f"DELETE FROM {table} WHERE guild_id = $1", guild_id)
    return {"deleted": True}
