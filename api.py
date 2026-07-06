"""
Fishie bot API — commands, stats, OAuth, and user data history.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from core import Fishie

app = FastAPI(title="Fishie API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "DELETE"])

bot_ref: "Fishie | None" = None
TABLE_MAP = {
    "avatars": "avatars",
    "username_logs": "username_logs",
    "display_name_logs": "display_name_logs",
    "discrim_logs": "discrim_logs",
    "nickname_logs": "nickname_logs",
}



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
    r = await pool.fetchval("SELECT 1 FROM opted_out WHERE user_id = $1 AND cardinality(items) > 0", user_id)
    return r is not None



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
        cmds.append({
            "name": cmd.qualified_name,
            "description": cmd.description or cmd.short_doc or "",
            "category": cmd.cog_name or "Uncategorized",
            "usage": cmd.usage or "",
            "aliases": aliases,
            "params": params,
        })
    return {"commands": sorted(cmds, key=lambda c: (c["category"], c["name"]))}


@app.get("/stats")
async def bot_stats():
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    import datetime
    pool = _check_pool()
    async with pool.acquire() as conn:
        start = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        avatars_total = await conn.fetchval("SELECT COUNT(*) FROM avatars")
        avatars_today = await conn.fetchval("SELECT COUNT(*) FROM avatars WHERE created_at >= $1", start)
        commands_total = await conn.fetchval("SELECT COUNT(*) FROM command_logs")
        commands_today = await conn.fetchval("SELECT COUNT(*) FROM command_logs WHERE created_at >= $1", start)
        usernames_total = await conn.fetchval("SELECT COUNT(*) FROM username_logs")
        usernames_today = await conn.fetchval("SELECT COUNT(*) FROM username_logs WHERE created_at >= $1", start)
        discrims_total = await conn.fetchval("SELECT COUNT(*) FROM discrim_logs")
        discrims_today = await conn.fetchval("SELECT COUNT(*) FROM discrim_logs WHERE created_at >= $1", start)
        nicknames_total = await conn.fetchval("SELECT COUNT(*) FROM nickname_logs")
        nicknames_today = await conn.fetchval("SELECT COUNT(*) FROM nickname_logs WHERE created_at >= $1", start)
        guild_names_total = await conn.fetchval("SELECT COUNT(*) FROM guild_name_logs")
        guild_names_today = await conn.fetchval("SELECT COUNT(*) FROM guild_name_logs WHERE created_at >= $1", start)
        member_joins_total = await conn.fetchval("SELECT COUNT(*) FROM member_join_logs")
        member_joins_today = await conn.fetchval("SELECT COUNT(*) FROM member_join_logs WHERE time >= $1", start)
        guild_icons_total = await conn.fetchval("SELECT COUNT(*) FROM guild_icons")
        guild_icons_today = await conn.fetchval("SELECT COUNT(*) FROM guild_icons WHERE created_at >= $1", start)
        guild_avatars_total = await conn.fetchval("SELECT COUNT(*) FROM guild_avatars")
        guild_avatars_today = await conn.fetchval("SELECT COUNT(*) FROM guild_avatars WHERE created_at >= $1", start)
    return {
        "guilds": len(bot_ref.guilds),
        "users": sum(g.member_count or 0 for g in bot_ref.guilds),
        "commands": len(bot_ref.commands),
        "uptime_seconds": (
            (datetime.datetime.now().astimezone() - bot_ref.start_time).total_seconds()
            if hasattr(bot_ref, "start_time") else 0
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
        "redirect_uri": "https://crygup.com/discord",
        "grant_type": "authorization_code",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post("https://discord.com/api/oauth2/token", data=data) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise HTTPException(400, f"OAuth exchange failed: {err}")
            token_data = await resp.json()
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            user_data = await resp.json()
    return {"user": user_data, "access_token": token_data["access_token"]}


@app.get("/user/{user_id}")
async def get_user_data(user_id: int):
    pool = _check_pool()
    async with pool.acquire() as conn:
        return {
            "user_id": user_id,
            "counts": {
                "avatars": await conn.fetchval("SELECT COUNT(*) FROM avatars WHERE user_id = $1", user_id),
                "usernames": await conn.fetchval("SELECT COUNT(*) FROM username_logs WHERE user_id = $1", user_id),
                "display_names": await conn.fetchval("SELECT COUNT(*) FROM display_name_logs WHERE user_id = $1", user_id),
                "discrims": await conn.fetchval("SELECT COUNT(*) FROM discrim_logs WHERE user_id = $1", user_id),
            }
        }


@app.get("/usernames/{user_id}")
async def get_usernames(user_id: int, page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=100)):
    if await _check_opted_out(user_id):
        raise HTTPException(403, "This user has opted out of data collection")
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM username_logs WHERE user_id = $1", user_id)
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            "SELECT id, username, created_at FROM username_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user_id, per_page, (page - 1) * per_page,
        )
    return {"items": [{"id": r["id"], "value": r["username"], "created_at": r["created_at"].isoformat()} for r in rows], "total": count, "page": page, "pages": pages}


@app.get("/display-names/{user_id}")
async def get_display_names(user_id: int, page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=100)):
    if await _check_opted_out(user_id):
        raise HTTPException(403, "This user has opted out of data collection")
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM display_name_logs WHERE user_id = $1", user_id)
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            "SELECT id, display_name, created_at FROM display_name_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user_id, per_page, (page - 1) * per_page,
        )
    return {"items": [{"id": r["id"], "value": r["display_name"], "created_at": r["created_at"].isoformat()} for r in rows], "total": count, "page": page, "pages": pages}


@app.get("/discrims/{user_id}")
async def get_discrims(user_id: int, page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=100)):
    if await _check_opted_out(user_id):
        raise HTTPException(403, "This user has opted out of data collection")
    pool = _check_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM discrim_logs WHERE user_id = $1", user_id)
        pages = max(1, (count + per_page - 1) // per_page)
        rows = await conn.fetch(
            "SELECT id, discrim, created_at FROM discrim_logs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user_id, per_page, (page - 1) * per_page,
        )
    return {"items": [{"id": r["id"], "value": r["discrim"], "created_at": r["created_at"].isoformat()} for r in rows], "total": count, "page": page, "pages": pages}


@app.delete("/user/{user_id}")
async def delete_user_data(user_id: int, table: str = Query(None), authorization: str = Header(None)):
    if not bot_ref:
        raise HTTPException(503, "Bot not ready")
    import aiohttp
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token")
    token = authorization[7:]
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            if resp.status != 200:
                raise HTTPException(401, "Invalid access token")
            me = await resp.json()
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only delete your own data")
    pool = _check_pool()
    tables = [table] if table else list(TABLE_MAP.keys())
    deleted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for t in tables:
                db_table = TABLE_MAP.get(t)
                if not db_table:
                    raise HTTPException(400, f"Invalid table: {t}")
                r = await conn.execute(f"DELETE FROM {db_table} WHERE user_id = $1", user_id)
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
        if m.name.lower() == q.lower() or (m.global_name and m.global_name.lower() == q.lower()):
            return {"user_id": str(m.id)}
    if members:
        return {"user_id": str(members[0].id)}
    raise HTTPException(404, f'No guild member matched "{q}". Try a Discord ID instead.')


@app.delete("/item/{table}/{user_id}")
async def delete_item(table: str, user_id: int, key: str = Query(...), authorization: str = Header(None)):
    """Delete a specific logged item. Token is the OAuth access token from login."""
    import aiohttp
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token")
    token = authorization[7:]
    # Verify token belongs to this user
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            if resp.status != 200:
                raise HTTPException(401, "Invalid access token")
            me = await resp.json()
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only delete your own data")
    pool = _check_pool()
    async with pool.acquire() as conn:
        db_table = TABLE_MAP.get(table)
        if not db_table:
            raise HTTPException(400, f"Invalid table: {table}")
        if table == "avatars":
            r = await conn.execute(f"DELETE FROM {db_table} WHERE user_id = $1 AND avatar_key = $2", user_id, key)
        else:
            try:
                row_id = int(key)
            except ValueError:
                raise HTTPException(400, "Invalid key — must be a numeric ID")
            r = await conn.execute(f"DELETE FROM {db_table} WHERE user_id = $1 AND id = $2", user_id, row_id)
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
            headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"},
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
