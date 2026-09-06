"""Auth helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp
import discord
from fastapi import (
    APIRouter,
    Cookie,
    Header,
    HTTPException,
    Query,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field

from utils.credentials import decrypt_credential, encrypt_credential

from . import state as api_state

router = APIRouter()


def _active_application_id() -> int:
    """Return the application ID belonging to the running bot instance.

    During normal operation :class:`core.bot.Fishie` exposes an
    ``active_application_id`` property.  Keep a configuration fallback for
    lightweight API tests and older bot objects that predate dual-instance
    support.
    """

    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    active_id = getattr(api_state.bot_ref, "active_application_id", None)
    if active_id is None:
        active_id = getattr(api_state.bot_ref, "active_bot_id", None)
    if active_id is None:
        active_id = api_state.bot_ref.config.get("ids", {}).get("bot_id")
    try:
        return int(active_id)
    except (TypeError, ValueError) as error:
        raise HTTPException(503, "Bot application ID is not configured") from error


def _active_client_secret() -> str:
    """Return the OAuth/challenge secret for the running bot instance."""

    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    active_secret = getattr(api_state.bot_ref, "oauth_client_secret", None)
    if active_secret:
        return str(active_secret)
    keys = api_state.bot_ref.config.get("keys", {})
    secret = keys.get("client_secret")
    if not isinstance(secret, str) or not secret:
        raise HTTPException(503, "Bot client secret is not configured")
    return secret


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


async def _create_web_session(
    user_id: int, discord_access_token: str, expires_in: int | None
) -> tuple[str, int]:
    """Create an opaque browser session and keep the Discord token server-side."""
    pool = api_state._check_pool()
    lifetime = max(
        60, min(int(expires_in or api_state.SESSION_MAX_AGE), api_state.SESSION_MAX_AGE)
    )
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
    pool = api_state._check_pool()
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


async def _verify_token(
    authorization: str | None = None, session_id: str | None = None
) -> dict:
    if session_id:
        token = await _session_access_token(session_id)
    elif authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        raise HTTPException(401, "Missing access token")
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with api_state.bot_ref.session.get(
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
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    guild = api_state.bot_ref.get_guild(guild_id)
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


class OAuthExchangePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=32, max_length=256)
    redirect_uri: str = "https://crygup.com/dashboard"


def _oauth_redirect_uri(value: str) -> str:
    if value not in {"https://crygup.com", "https://crygup.com/dashboard"}:
        raise HTTPException(400, "Invalid OAuth redirect URI")
    return value


@router.get("/oauth/start")
async def oauth_start(
    response: Response,
    redirect_uri: str = Query("https://crygup.com/dashboard"),
):
    """Create a one-time Discord OAuth state and PKCE verifier."""
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    redirect_uri = _oauth_redirect_uri(redirect_uri)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    pool = api_state._check_pool()
    await pool.execute("DELETE FROM oauth_states WHERE expires_at <= now()")
    await pool.execute(
        "INSERT INTO oauth_states (state_hash, code_verifier, redirect_uri, expires_at) "
        "VALUES ($1, $2, $3, now() + interval '10 minutes')",
        _session_hash(state),
        verifier,
        redirect_uri,
    )
    response.set_cookie(
        key=api_state.OAUTH_STATE_COOKIE,
        value=state,
        max_age=api_state.OAUTH_STATE_MAX_AGE,
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


@router.post("/oauth/exchange")
async def oauth_exchange(
    response: Response,
    payload: OAuthExchangePayload,
    oauth_state: str | None = Cookie(None, alias=api_state.OAUTH_STATE_COOKIE),
):
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    redirect_uri = _oauth_redirect_uri(payload.redirect_uri)
    if not oauth_state or not hmac.compare_digest(oauth_state, payload.state):
        raise HTTPException(400, "Invalid OAuth state")
    row = await api_state._check_pool().fetchrow(
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
    async with api_state.bot_ref.session.post(
        "https://discord.com/api/oauth2/token", data=data
    ) as resp:
        if resp.status != 200:
            raise HTTPException(400, "OAuth exchange failed")
        token_data = await resp.json()
    headers = {"Authorization": f"Bearer {token_data['access_token']}"}
    async with api_state.bot_ref.session.get(
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
        key=api_state.SESSION_COOKIE,
        value=session_id,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(key=api_state.OAUTH_STATE_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"user": user_data}


@router.post("/oauth/logout")
async def oauth_logout(
    response: Response,
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    if session_id:
        pool = api_state._check_pool()
        await pool.execute(
            "DELETE FROM web_sessions WHERE session_id_hash = $1",
            _session_hash(session_id),
        )
    response.delete_cookie(key=api_state.SESSION_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}


@router.get("/oauth/me")
async def oauth_me(
    response: Response,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
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
                response.delete_cookie(key=api_state.SESSION_COOKIE, path="/")
            return {"authenticated": False, "user": None}
        raise
    return {"authenticated": True, "user": user}
