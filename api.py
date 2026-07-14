"""
Fishie bot API — commands, stats, OAuth, and user data history.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import aiohttp
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core import Fishie

app = FastAPI(title="Fishie API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

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
LASTFM_ACCOUNT_FIELDS = ("lastfm", "steam", "roblox", "genshin", "letterboxd")


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


async def _refresh_discord_accounts_message(
    user_id: int, channel_id: int | None, message_id: int | None
) -> None:
    if not bot_ref or channel_id is None or message_id is None:
        return
    try:
        from extensions.settings import ManageAccountsView, _accounts_embed

        channel = bot_ref.get_channel(channel_id)
        if channel is None:
            channel = await bot_ref.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        row = await bot_ref.pool.fetchrow(
            "SELECT lastfm, steam, roblox, genshin, letterboxd FROM accounts "
            "WHERE user_id = $1",
            user_id,
        )
        author = bot_ref.get_user(user_id) or SimpleNamespace(id=user_id)
        ctx = SimpleNamespace(bot=bot_ref, author=author)
        await message.edit(
            embed=_accounts_embed(ctx, row),
            view=ManageAccountsView(ctx, lastfm_connected=True),
        )
    except Exception as error:
        bot_ref.logger.warning(
            "Could not refresh Discord accounts message after Last.fm OAuth: %s",
            error,
        )


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
    user_id: int, payload: dict = Body(...), authorization: str = Header(None)
):
    """Set the opted-out tracking methods. Requires OAuth bearer token."""
    import aiohttp

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token")
    token = authorization[7:]
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            "https://discord.com/api/users/@me", headers=headers
        ) as resp:
            if resp.status != 200:
                raise HTTPException(401, "Invalid access token")
            me = await resp.json()
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


async def _verify_token(authorization: str | None) -> dict:
    import aiohttp

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token")
    token = authorization[7:]
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
async def get_user_guilds(user_id: int, authorization: str = Header(None)):
    """Get guilds where the user has Manage Server. Requires OAuth."""
    me = await _verify_token(authorization)
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
    guild_id: int, payload: dict = Body(...), authorization: str = Header(None)
):
    """Set opted-out tracking for a guild. Requires OAuth + Manage Server."""
    me = await _verify_token(authorization)
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
    return {"user": user_data, "access_token": token_data["access_token"]}


@app.get("/lastfm/connect")
async def lastfm_connect(authorization: str = Header(None)):
    """Create a Last.fm authorization URL for the authenticated Discord user."""
    me = await _verify_token(authorization)
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
    if not username:
        raise HTTPException(502, "Last.fm did not return a username")

    pool = _check_pool()
    await pool.execute(
        """INSERT INTO accounts (user_id, lastfm)
           VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE
           SET lastfm = EXCLUDED.lastfm;
        """,
        user_id,
        username,
    )
    bot_ref.db_cache.add_account(user_id, username)
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
async def get_user_xp(user_id: int, authorization: str = Header(None)):
    """Get XP and message count for a user. Requires OAuth."""
    me = await _verify_token(authorization)
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
    user_id: int, table: str = Query(None), authorization: str = Header(None)
):
    import aiohttp

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token")
    token = authorization[7:]
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            "https://discord.com/api/users/@me", headers=headers
        ) as resp:
            if resp.status != 200:
                raise HTTPException(401, "Invalid access token")
            me = await resp.json()

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
        raise HTTPException(400, "Username lookup not available — use a Discord ID")
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
    table: str, user_id: int, key: str = Query(...), authorization: str = Header(None)
):
    """Delete a specific logged item. Token is the OAuth access token from login."""
    import aiohttp

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token")
    token = authorization[7:]
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            "https://discord.com/api/users/@me", headers=headers
        ) as resp:
            if resp.status != 200:
                raise HTTPException(401, "Invalid access token")
            me = await resp.json()

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
async def get_user_reminders(user_id: int, authorization: str = Header(None)):
    """Get reminders for a user. Requires OAuth."""
    me = await _verify_token(authorization)
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
async def get_user_accounts(user_id: int, authorization: str = Header(None)):
    """Get connected accounts for a user. Requires OAuth."""
    me = await _verify_token(authorization)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own accounts")
    pool = _check_pool()
    row = await pool.fetchrow("SELECT * FROM accounts WHERE user_id = $1", user_id)
    if not row:
        return {"accounts": {}}
    return {
        "accounts": {field: row[field] for field in LASTFM_ACCOUNT_FIELDS if row[field]}
    }


@app.post("/user/{user_id}/accounts")
async def set_user_accounts(
    user_id: int, payload: dict = Body(...), authorization: str = Header(None)
):
    """Set connected accounts. Requires OAuth."""
    me = await _verify_token(authorization)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    # Last.fm can only be changed through its authorization flow.
    allowed = {"steam", "roblox", "genshin", "letterboxd"}
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
async def disconnect_lastfm(user_id: int, authorization: str = Header(None)):
    """Disconnect Last.fm for the authenticated Discord user."""
    me = await _verify_token(authorization)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "Not your account")
    pool = _check_pool()
    await pool.execute(
        "UPDATE accounts SET lastfm = NULL WHERE user_id = $1",
        user_id,
    )
    if bot_ref:
        bot_ref.db_cache.lastfm.pop(user_id, None)
    return {"disconnected": True}


@app.get("/guild/{guild_id}/settings")
async def get_guild_settings(guild_id: int, authorization: str = Header(None)):
    """Get guild settings. Requires OAuth."""
    me = await _verify_token(authorization)
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
    guild_id: int, payload: dict = Body(...), authorization: str = Header(None)
):
    me = await _verify_token(authorization)
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
    guild_id: int, payload: dict = Body(...), authorization: str = Header(None)
):
    """Add a custom prefix. Requires OAuth + Manage Server."""
    try:
        me = await _verify_token(authorization)
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
    guild_id: int, payload: dict = Body(...), authorization: str = Header(None)
):
    """Remove a custom prefix. Requires OAuth + Manage Server."""
    me = await _verify_token(authorization)
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
async def delete_guild_data(guild_id: int, authorization: str = Header(None)):
    """Delete all tracking data for a guild. Requires OAuth + Manage Server."""
    me = await _verify_token(authorization)
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
