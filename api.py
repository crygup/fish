"""
Fishie bot API — commands, stats, OAuth, and user data history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import time

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
    for cmd in bot_ref.commands:
        if cmd.hidden:
            continue
        if cmd.cog_name == "Owner":
            continue
        aliases = ", ".join(cmd.aliases) if cmd.aliases else ""
        sig = ""
        params = []
        for name, param in cmd.clean_params.items():
            req = "required" if param.default is param.empty else "optional"
            params.append({"name": name, "required": req})
        cmds.append(
            {
                "name": cmd.qualified_name,
                "description": cmd.description or cmd.short_doc or "",
                "category": cmd.cog_name or "Uncategorized",
                "usage": cmd.usage or "",
                "aliases": aliases,
                "params": params,
            }
        )
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


@app.post("/oauth/exchange")
async def oauth_exchange(code: str = Query(...)):
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    data = {
        "client_id": str(bot_ref.config["ids"]["bot_id"]),
        "client_secret": bot_ref.config["keys"]["client_secret"],
        "code": code,
        "redirect_uri": "https://crygup.com",
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
