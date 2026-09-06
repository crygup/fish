"""Accounts helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import discord
from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)

from utils.credentials import decrypt_credential, encrypt_credential

from . import auth as api_auth
from . import state as api_state

router = APIRouter()


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
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(api_state.bot_ref, "_lastfm_oauth_states", None)
    if states is None:
        states = api_state.bot_ref._lastfm_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)

    token = secrets.token_urlsafe(32)
    state_data: dict[str, Any] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + api_state.LASTFM_STATE_TTL,
    }
    if session_id:
        state_data["session_hash"] = api_auth._session_hash(session_id)
    if browser_nonce:
        state_data["browser_nonce_hash"] = api_auth._session_hash(browser_nonce)
    states[token] = state_data
    return f"lastfm_{token}"


def _lastfm_authorization_url(
    user_id: int,
    source: str,
    *,
    session_id: str | None = None,
    browser_nonce: str | None = None,
) -> str:
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    state = _lastfm_state(
        user_id,
        source,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    callback = f"{api_state.LASTFM_CALLBACK_URL}?{urlencode({'lastfm_state': state})}"
    return "https://www.last.fm/api/auth/?" + urlencode(
        {"api_key": api_state.bot_ref.config["keys"]["lastfm_cb"], "cb": callback}
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
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(api_state.bot_ref, "_steam_oauth_states", None)
    if states is None:
        states = api_state.bot_ref._steam_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    states[token] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + api_state.STEAM_STATE_TTL,
    }
    if session_id:
        states[token]["session_hash"] = api_auth._session_hash(session_id)
    if browser_nonce:
        states[token]["browser_nonce_hash"] = api_auth._session_hash(browser_nonce)
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
    callback = f"{api_state.STEAM_CALLBACK_URL}?{urlencode({'steam_state': state})}"
    return (
        api_state.STEAM_OPENID_URL
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
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    states = getattr(api_state.bot_ref, "_steam_oauth_states", None) or {}
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
        if (
            not expected_session
            or not session_id
            or not hmac.compare_digest(
                str(expected_session), api_auth._session_hash(session_id)
            )
        ):
            raise HTTPException(400, "Invalid Steam browser session")
        if (
            not expected_nonce
            or not browser_nonce
            or not hmac.compare_digest(
                str(expected_nonce), api_auth._session_hash(browser_nonce)
            )
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
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(api_state.bot_ref, "_spotify_oauth_states", None)
    if states is None:
        states = api_state.bot_ref._spotify_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    states[token] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + api_state.SPOTIFY_STATE_TTL,
    }
    if session_id:
        states[token]["session_hash"] = api_auth._session_hash(session_id)
    if browser_nonce:
        states[token]["browser_nonce_hash"] = api_auth._session_hash(browser_nonce)
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
            "client_id": (
                api_state.bot_ref.config["keys"]["spotify_id"]
                if api_state.bot_ref
                else ""
            ),
            "response_type": "code",
            "redirect_uri": api_state.SPOTIFY_CALLBACK_URL,
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
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    if not state.startswith("spotify_"):
        raise HTTPException(400, "Invalid Spotify connection state")
    states = getattr(api_state.bot_ref, "_spotify_oauth_states", None) or {}
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
        if (
            not expected_session
            or not session_id
            or not hmac.compare_digest(
                str(expected_session), api_auth._session_hash(session_id)
            )
        ):
            raise HTTPException(400, "Invalid Spotify browser session")
        if (
            not expected_nonce
            or not browser_nonce
            or not hmac.compare_digest(
                str(expected_nonce), api_auth._session_hash(browser_nonce)
            )
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
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    now = int(time.time())
    states = getattr(api_state.bot_ref, "_anilist_oauth_states", None)
    if states is None:
        states = api_state.bot_ref._anilist_oauth_states = {}
    for token, state_data in list(states.items()):
        if int(state_data.get("expires", 0)) < now:
            states.pop(token, None)
    token = secrets.token_urlsafe(24)
    states[token] = {
        "user_id": int(user_id),
        "source": source,
        "expires": now + api_state.ANILIST_STATE_TTL,
    }
    if session_id:
        states[token]["session_hash"] = api_auth._session_hash(session_id)
    if browser_nonce:
        states[token]["browser_nonce_hash"] = api_auth._session_hash(browser_nonce)
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
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    state = _anilist_state(
        user_id,
        source,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    return "https://anilist.co/api/v2/oauth/authorize?" + urlencode(
        {
            "client_id": api_state.bot_ref.config["keys"]["anilist_id"],
            "redirect_uri": api_state.ANILIST_CALLBACK_URL,
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
    if not api_state.bot_ref or not state.startswith("anilist_"):
        raise HTTPException(400, "Invalid AniList connection state")
    states = getattr(api_state.bot_ref, "_anilist_oauth_states", None) or {}
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
        if (
            not expected_session
            or not session_id
            or not hmac.compare_digest(
                str(expected_session), api_auth._session_hash(session_id)
            )
        ):
            raise HTTPException(400, "Invalid AniList browser session")
        if (
            not expected_nonce
            or not browser_nonce
            or not hmac.compare_digest(
                str(expected_nonce), api_auth._session_hash(browser_nonce)
            )
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
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")

    # States issued by current versions are opaque and removed before any
    # network request is made, making callback handling one-time even when two
    # requests arrive concurrently.
    if state.startswith("lastfm_"):
        states = getattr(api_state.bot_ref, "_lastfm_oauth_states", None) or {}
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
                str(expected_session), api_auth._session_hash(session_id)
            ):
                raise HTTPException(400, "Invalid Last.fm browser session")
        if expected_nonce:
            if not browser_nonce or not hmac.compare_digest(
                str(expected_nonce), api_auth._session_hash(browser_nonce)
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
    if legacy_key in api_state._legacy_lastfm_states_used:
        raise HTTPException(400, "Invalid or already used Last.fm connection state")
    try:
        encoded, supplied_signature = state.split(".", 1)
        expected_signature = hmac.new(
            api_state.bot_ref.config["keys"]["lastfm_secret"].encode(),
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
    api_state._legacy_lastfm_states_used[legacy_key] = True
    return user_id, source, channel_id, message_id


def _lastfm_api_signature(api_key: str, token: str, secret: str) -> str:
    signature = f"api_key{api_key}methodauth.getSessiontoken{token}{secret}"
    return hashlib.md5(signature.encode()).hexdigest()


async def _steam_display_name(steam_id: str) -> str | None:
    if not api_state.bot_ref:
        return None
    api_key = api_state.bot_ref.config["keys"].get("steam")
    if not api_key:
        return None
    try:
        async with api_state.bot_ref.session.get(
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
    if not api_state.bot_ref or channel_id is None or message_id is None:
        return
    try:
        from extensions.settings import ManageAccountsView

        channel: Any = api_state.bot_ref.get_channel(channel_id)
        if channel is None:
            channel = await api_state.bot_ref.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        row = await api_state.bot_ref.pool.fetchrow(
            "SELECT lastfm, steam, roblox, letterboxd, anilist FROM accounts "
            "WHERE user_id = $1",
            user_id,
        )
        author = api_state.bot_ref.get_user(user_id) or SimpleNamespace(id=user_id)
        ctx = SimpleNamespace(bot=api_state.bot_ref, author=author)
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
        api_state.bot_ref.logger.warning(
            "Could not refresh Discord accounts message after account OAuth: %s",
            error,
        )


def _schedule_discord_accounts_refresh(
    user_id: int, channel_id: int | None, message_id: int | None
) -> None:
    if not api_state.bot_ref or channel_id is None or message_id is None:
        return
    tasks = api_state.bot_ref._oauth_refresh_tasks
    task = asyncio.create_task(
        _refresh_discord_accounts_message(user_id, channel_id, message_id)
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)


@router.get("/lastfm/connect")
async def lastfm_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Create a Last.fm authorization URL for the authenticated Discord user."""
    me = await api_auth._verify_token(authorization, session_id)
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
        key=api_state.LASTFM_STATE_COOKIE,
        value=browser_nonce,
        max_age=api_state.LASTFM_STATE_TTL,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return {"url": state_url}


@router.get("/lastfm/callback")
async def lastfm_callback(
    response: Response,
    token: str = Query(...),
    state: str = Query(...),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=api_state.LASTFM_STATE_COOKIE),
):
    """Exchange a Last.fm callback token and persist the verified account."""
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise HTTPException(400, "Invalid Last.fm authentication token")

    user_id, source, channel_id, message_id = _decode_lastfm_state(
        state,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    api_key = api_state.bot_ref.config["keys"]["lastfm_cb"]
    api_secret = api_state.bot_ref.config["keys"]["lastfm_cb_secret"]
    payload = {
        "method": "auth.getSession",
        "api_key": api_key,
        "token": token,
        "api_sig": _lastfm_api_signature(api_key, token, api_secret),
        "format": "json",
    }
    async with api_state.bot_ref.session.post(
        api_state.LASTFM_API_URL, data=payload
    ) as resp:
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

    pool = api_state._check_pool()
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
    await api_state.bot_ref.refresh_account_cache(user_id)
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    response.delete_cookie(key=api_state.LASTFM_STATE_COOKIE, path="/")
    return {"username": username, "source": source}


@router.get("/steam/connect")
async def steam_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Create a Steam OpenID URL for the authenticated Discord user."""
    me = await api_auth._verify_token(authorization, session_id)
    if not session_id:
        raise HTTPException(401, "A browser session is required to connect Steam")
    browser_nonce = secrets.token_urlsafe(32)
    response.headers["Cache-Control"] = "private, no-store"
    response.set_cookie(
        key=api_state.STEAM_STATE_COOKIE,
        value=browser_nonce,
        max_age=api_state.STEAM_STATE_TTL,
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


@router.get("/steam/callback")
async def steam_callback(
    response: Response,
    request: Request,
    steam_state: str = Query(...),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=api_state.STEAM_STATE_COOKIE),
):
    """Verify a Steam OpenID response and persist the user's SteamID64."""
    if not api_state.bot_ref:
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
    async with api_state.bot_ref.session.post(
        api_state.STEAM_OPENID_URL, data=verify_payload
    ) as resp:
        verification = await resp.text()
    if resp.status != 200 or not re.search(
        r"(?:^|\n)is_valid:true(?:\r?\n|$)", verification
    ):
        raise HTTPException(400, "Steam authorization could not be verified")

    steam_id = match.group(1)
    persona_name = None
    api_key = api_state.bot_ref.config["keys"].get("steam")
    if api_key:
        try:
            async with api_state.bot_ref.session.get(
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
            api_state.bot_ref.logger.warning(
                "Steam profile lookup failed for linked account"
            )

    pool = api_state._check_pool()
    await pool.execute(
        """INSERT INTO accounts (user_id, steam)
           VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE
           SET steam = EXCLUDED.steam;""",
        user_id,
        steam_id,
    )
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    response.delete_cookie(key=api_state.STEAM_STATE_COOKIE, path="/")
    return {"steamid": steam_id, "personaname": persona_name, "source": source}


@router.get("/spotify/connect")
async def spotify_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Create a Spotify authorization URL for the authenticated user."""
    me = await api_auth._verify_token(authorization, session_id)
    if not session_id:
        raise HTTPException(401, "A browser session is required to connect Spotify")
    browser_nonce = secrets.token_urlsafe(32)
    response.set_cookie(
        key=api_state.SPOTIFY_STATE_COOKIE,
        value=browser_nonce,
        max_age=api_state.SPOTIFY_STATE_TTL,
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


@router.get("/spotify/callback")
async def spotify_callback(
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=api_state.SPOTIFY_STATE_COOKIE),
):
    """Exchange a Spotify authorization code and persist the user's account."""
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_spotify_state(
        state,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    api_state.bot_ref.logger.info(
        "Spotify OAuth callback accepted source=%s user_id=%s", source, user_id
    )
    auth = aiohttp.BasicAuth(
        api_state.bot_ref.config["keys"]["spotify_id"],
        api_state.bot_ref.config["keys"]["spotify_secret"],
    )
    try:
        async with api_state.bot_ref.session.post(
            api_state.SPOTIFY_TOKEN_URL,
            auth=auth,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": api_state.SPOTIFY_CALLBACK_URL,
            },
        ) as token_response:
            try:
                token_data = await token_response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                raise HTTPException(400, "Spotify returned an invalid token response")
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        api_state.bot_ref.logger.warning(
            "Spotify token exchange unavailable: %s", error
        )
        raise HTTPException(400, "Spotify authorization service unavailable") from error
    api_state.bot_ref.logger.info(
        "Spotify OAuth token exchange completed status=%s user_id=%s",
        token_response.status,
        user_id,
    )
    if token_response.status != 200 or not isinstance(token_data, dict):
        error_code = token_data.get("error") if isinstance(token_data, dict) else None
        api_state.bot_ref.logger.warning(
            "Spotify OAuth token exchange rejected status=%s error=%s user_id=%s",
            token_response.status,
            error_code or "unknown",
            user_id,
        )
        raise HTTPException(400, "Spotify authorization failed")
    access_token = token_data.get("access_token")
    issued_refresh_token = token_data.get("refresh_token")
    api_state.bot_ref.logger.info(
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
    pool = api_state._check_pool()
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
            api_state.bot_ref.logger.warning(
                "Spotify stored refresh-token lookup timed out user_id=%s", user_id
            )
            raise HTTPException(400, "Spotify connection temporarily unavailable")
    if not refresh_token:
        api_state.bot_ref.logger.warning(
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
        api_state.bot_ref.logger.warning(
            "Spotify refresh-token persistence timed out user_id=%s", user_id
        )
        raise HTTPException(400, "Spotify connection temporarily unavailable")
    api_state.bot_ref.logger.info("Spotify refresh token persisted user_id=%s", user_id)

    try:
        api_state.bot_ref.logger.info(
            "Spotify OAuth profile lookup started user_id=%s", user_id
        )
        async with api_state.bot_ref.session.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as profile_response:
            profile_status = profile_response.status
            profile_content_type = profile_response.headers.get("Content-Type", "")
            profile_body = await profile_response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        api_state.bot_ref.logger.warning(
            "Spotify profile lookup unavailable: %s", error
        )
        raise HTTPException(400, "Spotify profile service unavailable") from error
    api_state.bot_ref.logger.info(
        "Spotify OAuth profile response status=%s content_type=%s bytes=%s user_id=%s",
        profile_status,
        profile_content_type.split(";", 1)[0] or "unknown",
        len(profile_body),
        user_id,
    )
    try:
        profile = json.loads(profile_body)
    except (TypeError, ValueError):
        api_state.bot_ref.logger.warning(
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
        api_state.bot_ref.logger.warning(
            "Spotify profile persistence timed out user_id=%s", user_id
        )
        raise HTTPException(400, "Spotify connection temporarily unavailable")
    _schedule_discord_accounts_refresh(user_id, channel_id, message_id)
    api_state.bot_ref.logger.info("Spotify account linked user_id=%s", user_id)
    response.delete_cookie(key=api_state.SPOTIFY_STATE_COOKIE, path="/")
    return {"display_name": display_name, "source": source}


@router.get("/anilist/connect")
async def anilist_connect(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Create an AniList authorization URL for the authenticated Discord user."""
    me = await api_auth._verify_token(authorization, session_id)
    if not session_id:
        raise HTTPException(401, "A browser session is required to connect AniList")
    browser_nonce = secrets.token_urlsafe(32)
    response.set_cookie(
        key=api_state.ANILIST_STATE_COOKIE,
        value=browser_nonce,
        max_age=api_state.ANILIST_STATE_TTL,
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


@router.get("/anilist/callback")
async def anilist_callback(
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
    browser_nonce: str | None = Cookie(None, alias=api_state.ANILIST_STATE_COOKIE),
):
    """Exchange an AniList code and persist the verified profile credentials."""
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    user_id, source, channel_id, message_id = _decode_anilist_state(
        state,
        session_id=session_id,
        browser_nonce=browser_nonce,
    )
    payload = {
        "grant_type": "authorization_code",
        "client_id": api_state.bot_ref.config["keys"]["anilist_id"],
        "client_secret": api_state.bot_ref.config["keys"]["anilist_secret"],
        "redirect_uri": api_state.ANILIST_CALLBACK_URL,
        "code": code,
    }
    async with api_state.bot_ref.session.post(
        api_state.ANILIST_TOKEN_URL, json=payload
    ) as token_response:
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
    async with api_state.bot_ref.session.post(
        api_state.ANILIST_GRAPHQL_URL,
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

    pool = api_state._check_pool()
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
    await api_state.bot_ref.refresh_account_cache(user_id)
    await _refresh_discord_accounts_message(user_id, channel_id, message_id)
    response.delete_cookie(key=api_state.ANILIST_STATE_COOKIE, path="/")
    return {"username": username, "source": source}


@router.get("/user/{user_id}/reminders")
async def get_user_reminders(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get reminders for a user. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own reminders")
    pool = api_state._check_pool()
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


@router.get("/user/{user_id}/first-command")
async def get_user_first_command(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get the date of a user's first command use."""
    await api_auth._require_self(user_id, authorization, session_id)
    pool = api_state._check_pool()
    row = await pool.fetchrow(
        "SELECT created_at FROM command_logs WHERE user_id = $1 ORDER BY created_at ASC LIMIT 1",
        user_id,
    )
    return {"first_command": str(row["created_at"]) if row else None}


@router.get("/user/{user_id}/accounts")
async def get_user_accounts(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get connected accounts for a user. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own accounts")
    pool = api_state._check_pool()
    row = await pool.fetchrow("SELECT * FROM accounts WHERE user_id = $1", user_id)
    if not row:
        return {"accounts": {}}
    accounts = {
        field: row[field] for field in api_state.LASTFM_ACCOUNT_FIELDS if row[field]
    }
    if row["steam"]:
        display_name = await _steam_display_name(row["steam"])
        if display_name:
            accounts["steam_display_name"] = display_name
    return {"accounts": accounts}


@router.post("/user/{user_id}/accounts")
async def set_user_accounts(
    user_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Set connected accounts. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    # Last.fm and Steam can only be changed through their authorization flows.
    allowed = {"roblox", "letterboxd"}
    accounts = {k: v for k, v in payload.get("accounts", {}).items() if k in allowed}
    pool = api_state._check_pool()
    if accounts:
        keys = ", ".join(accounts.keys())
        vals = ", ".join(f"${i + 2}" for i in range(len(accounts)))
        placeholders = list(accounts.values())
        await pool.execute(
            f"INSERT INTO accounts (user_id, {keys}) VALUES ($1, {vals}) ON CONFLICT (user_id) DO UPDATE SET {', '.join(f'{k}=EXCLUDED.{k}' for k in accounts)}",
            user_id,
            *placeholders,
        )
    return {"accounts": accounts}


@router.delete("/user/{user_id}/lastfm")
async def disconnect_lastfm(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Disconnect Last.fm for the authenticated Discord user."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    pool = api_state._check_pool()
    await pool.execute(
        "UPDATE accounts SET lastfm = NULL, lastfm_session_key = NULL WHERE user_id = $1",
        user_id,
    )
    if api_state.bot_ref:
        await api_state.bot_ref.refresh_account_cache(user_id)
    return {"disconnected": True}


@router.delete("/user/{user_id}/steam")
async def disconnect_steam(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Disconnect Steam for the authenticated Discord user."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    pool = api_state._check_pool()
    await pool.execute("UPDATE accounts SET steam = NULL WHERE user_id = $1", user_id)
    return {"disconnected": True}


@router.delete("/user/{user_id}/anilist")
async def disconnect_anilist(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Disconnect AniList for the authenticated Discord user."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    pool = api_state._check_pool()
    await pool.execute(
        "UPDATE accounts SET anilist = NULL, anilist_access_token = NULL "
        "WHERE user_id = $1",
        user_id,
    )
    if api_state.bot_ref:
        await api_state.bot_ref.refresh_account_cache(user_id)
    return {"disconnected": True}
