"""Public helpers and endpoints for Fishie."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from typing import Any, cast
from urllib.parse import urlsplit

import aiohttp
from discord.ext import commands
from fastapi import (
    APIRouter,
    Cookie,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from pydantic import BaseModel, Field

from utils.network import (
    DISCORD_ATTACHMENT_HOSTS,
    validate_public_url,
)

from . import auth as api_auth
from . import state as api_state

router = APIRouter()


@router.get("/commands")
async def list_commands():
    if not api_state.bot_ref:
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

    for cmd in api_state.bot_ref.commands:
        add_cmd(cmd)
        if hasattr(cmd, "walk_commands"):
            for sub in cast(Any, cmd).walk_commands():
                add_cmd(sub)
    return {"commands": sorted(cmds, key=lambda c: (c["category"], c["name"]))}


_stats_cache: tuple[float, dict[str, Any]] | None = None


@router.get("/stats")
async def bot_stats():
    global _stats_cache
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    now_monotonic = time.monotonic()
    if _stats_cache and now_monotonic - _stats_cache[0] < 300:
        return _stats_cache[1]
    import datetime

    async with api_state._stats_lock:
        now_monotonic = time.monotonic()
        if _stats_cache and now_monotonic - _stats_cache[0] < 300:
            return _stats_cache[1]
        pool = api_state._check_pool()
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
            "guilds": len(api_state.bot_ref.guilds),
            "users": sum(g.member_count or 0 for g in api_state.bot_ref.guilds),
            "commands": len(api_state.bot_ref.commands),
            "uptime_seconds": (
                (
                    datetime.datetime.now().astimezone() - api_state.bot_ref.start_time
                ).total_seconds()
                if hasattr(api_state.bot_ref, "start_time")
                else 0
            ),
            "today": {name: row["today"] for name, row in by_name.items()},
            "totals": {name: row["total"] for name, row in by_name.items()},
        }
        _stats_cache = (now_monotonic, result)
        return result


def _require_website_origin(request: Request) -> None:
    """Require browser requests to originate from the Fishie website.

    CORS controls whether a browser can read a response; it does not prevent
    arbitrary clients from posting directly.  These public website endpoints
    therefore require the exact origin as an additional request boundary.
    """

    if request.headers.get("origin") not in api_state.WEB_ORIGINS:
        raise HTTPException(403, "This endpoint is available from the Fishie website")


def _normalise_spotify_query(value: str) -> str:
    return " ".join(value.casefold().split())


def _check_spotify_cover_rate(ip: str) -> None:
    now = time.monotonic()
    timestamps = [
        timestamp
        for timestamp in (api_state._spotify_cover_rate.get(ip) or [])
        if now - timestamp < api_state.SPOTIFY_COVER_RATE_WINDOW
    ]
    if len(timestamps) >= api_state.SPOTIFY_COVER_RATE_LIMIT:
        retry_after = max(
            1,
            int(api_state.SPOTIFY_COVER_RATE_WINDOW - (now - timestamps[0])) + 1,
        )
        raise HTTPException(
            429,
            "Too many cover lookups; please try again later",
            headers={"Retry-After": str(retry_after)},
        )
    timestamps.append(now)
    api_state._spotify_cover_rate[ip] = timestamps


@router.get("/spotify-cover")
async def spotify_cover(
    request: Request,
    artist: str = Query(..., min_length=1, max_length=200),
    track: str = Query(..., min_length=1, max_length=200),
):
    """Search Spotify for a track cover image. Falls back if Last.fm has no cover."""
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    _require_website_origin(request)
    ip = _client_ip(request)
    _check_spotify_cover_rate(ip)
    sid = api_state.bot_ref.config["keys"]["spotify_id"]
    ss = api_state.bot_ref.config["keys"]["spotify_secret"]
    encoded = base64.b64encode(f"{sid}:{ss}".encode("ascii")).decode("ascii")
    cache_key = (_normalise_spotify_query(artist), _normalise_spotify_query(track))
    if cached := api_state._spotify_cover_cache.get(cache_key):
        return {"url": cached}
    if cache_key in api_state._spotify_cover_negative_cache:
        raise HTTPException(404, "No cover found")

    # Limit concurrent Spotify calls and avoid a token stampede when the
    # process starts with no cached client-credentials token.
    async with api_state._spotify_cover_semaphore:
        token = api_state.bot_ref.spotify_key
        if token is None:
            async with api_state._spotify_cover_token_lock:
                token = api_state.bot_ref.spotify_key
                if token is None:
                    async with api_state.bot_ref.session.post(
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
                    api_state.bot_ref.spotify_key = token
        headers = {"Authorization": f"Bearer {token}"}
        q = f"track:{track} artist:{artist}"
        async with api_state.bot_ref.session.get(
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
        api_state._spotify_cover_negative_cache[cache_key] = True
        raise HTTPException(404, "No cover found")
    api_state._spotify_cover_cache[cache_key] = cover_url
    return {"url": cover_url}


class MessagePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=1, max_length=2000)
    avatar_url: str | None = Field(None, max_length=2048)
    discord_id: str | None = Field(None, pattern=r"^[0-9]{1,20}$")


def _message_challenge_secret() -> bytes:
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    keys = api_state.bot_ref.config.get("keys", {})
    # Keep the challenge independent from the public media key when one is
    # configured, while allowing existing deployments to use their server
    # secret without a new environment variable.
    secret = (
        keys.get("message_challenge")
        or keys.get("message_challenge_secret")
        or api_auth._active_client_secret()
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
    if abs(time.time() - issued) > api_state.MESSAGE_CHALLENGE_MAX_AGE:
        raise HTTPException(403, "Message challenge expired")
    expected_ip = hmac.new(secret, ip.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(ip_digest, expected_ip):
        raise HTTPException(403, "Message challenge does not match this client")
    token_hash = hashlib.sha256(supplied.encode()).hexdigest()
    if token_hash in api_state._message_used_challenges:
        raise HTTPException(403, "Message challenge has already been used")
    return token_hash


async def _consume_message_challenge(token_hash: str, ip: str) -> None:
    now = time.monotonic()
    async with api_state._message_rate_lock:
        if token_hash in api_state._message_used_challenges:
            raise HTTPException(403, "Message challenge has already been used")
        if ip in api_state._msg_rate_limit:
            raise HTTPException(
                429,
                "Please wait before sending another message",
                headers={"Retry-After": "60"},
            )
        while (
            api_state._message_global_requests
            and now - api_state._message_global_requests[0]
            >= api_state.MESSAGE_GLOBAL_RATE_WINDOW
        ):
            api_state._message_global_requests.popleft()
        if (
            len(api_state._message_global_requests)
            >= api_state.MESSAGE_GLOBAL_RATE_LIMIT
        ):
            retry_after = max(
                1,
                int(
                    api_state.MESSAGE_GLOBAL_RATE_WINDOW
                    - (now - api_state._message_global_requests[0])
                )
                + 1,
            )
            raise HTTPException(
                429,
                "Message service is busy; please try again later",
                headers={"Retry-After": str(retry_after)},
            )
        api_state._message_global_requests.append(now)
        api_state._msg_rate_limit[ip] = True
        api_state._message_used_challenges[token_hash] = True


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


@router.get("/send-message/challenge")
async def send_message_challenge(response: Response, request: Request):
    """Issue a short-lived, one-time token for the public website form."""

    _require_website_origin(request)
    ip = _client_ip(request)
    token = _message_challenge(ip)
    response.set_cookie(
        key=api_state.MESSAGE_CHALLENGE_COOKIE,
        value=token,
        max_age=api_state.MESSAGE_CHALLENGE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "expires_in": api_state.MESSAGE_CHALLENGE_MAX_AGE}


@router.post("/send-message")
async def send_message(
    payload: MessagePayload,
    request: Request,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
    challenge: str | None = Header(None, alias="X-Fishie-Message-Challenge"),
    challenge_cookie: str | None = Cookie(
        None, alias=api_state.MESSAGE_CHALLENGE_COOKIE
    ),
):
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")

    _require_website_origin(request)

    webhook_url = api_state.bot_ref.config["webhooks"].get("messages", "")
    if not webhook_url:
        raise HTTPException(500, "Webhook not configured")

    ip = _client_ip(request)

    if ip in api_state.bot_ref.cached_banned_ips:
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
        user = await api_auth._verify_token(authorization, session_id)
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
                api_auth._active_client_secret().encode(),
                ip.encode(),
                hashlib.sha256,
            ).hexdigest()[:16]
        },
        "color": 0xFAA0C1,
    }
    if payload.discord_id:
        embed["footer"]["text"] += f" · ID: {payload.discord_id}"

    async with api_state.bot_ref.session.post(
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


@router.get("/ror2-items")
async def get_ror2_items():
    """Return all RoR2 items from the database."""
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    pool = api_state.bot_ref.pool
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
