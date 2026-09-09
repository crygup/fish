"""History helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Header,
    HTTPException,
    Query,
)

from core.deletions import deletion, delete_account, restore, pending_status, invalidate, archive_badge_document
from utils.vars import remove_user_badge

from . import auth as api_auth
from . import state as api_state

router = APIRouter()


async def _check_opted_out(user_id: int) -> bool:
    pool = api_state._check_pool()
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
        await api_state._check_pool().fetchval(
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
    if (
        api_state.bot_ref is not None
        and user_id not in api_state.bot_ref.db_cache.known_non_bot_users
    ):
        cached_user = api_state.bot_ref.get_user(user_id)
        if (
            cached_user is None
            and user_id not in api_state._discord_user_negative_cache
        ):
            try:
                cached_user = await api_state.bot_ref.fetch_user(user_id)
            except (discord.HTTPException, discord.NotFound, asyncio.TimeoutError):
                # Invalid IDs and transient Discord failures should not cause a
                # fresh API request on every public history lookup.  Keep this
                # negative result short-lived so a newly-created user can still
                # be resolved later.
                api_state._discord_user_negative_cache[user_id] = True
                cached_user = None
        if cached_user is not None:
            api_state.bot_ref.db_cache.remember_user(
                cached_user.id, is_bot=cached_user.bot
            )
            if cached_user.bot:
                return

    history_public = await api_state._check_pool().fetchval(
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
        viewer_id = await api_state._check_pool().fetchval(
            """
            SELECT user_id
            FROM web_sessions
            WHERE session_id_hash = $1 AND expires_at > now()
            """,
            api_auth._session_hash(session_id),
        )
    # If a browser retained an expired/stale cookie, a valid bearer token must
    # still be honored for API clients rather than being masked by that cookie.
    if viewer_id is None and authorization:
        try:
            viewer = await api_auth._verify_token(authorization, None)
        except HTTPException:
            viewer = None
        if viewer is not None:
            viewer_id = int(viewer["id"])

    if viewer_id != user_id:
        raise HTTPException(403, "This user has made their saved history private")


@router.get("/user/{user_id}/opted-out")
async def get_opted_out(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get the list of tracking methods this user has opted out of."""
    await api_auth._require_self(user_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.post("/user/{user_id}/opted-out")
async def set_opted_out(
    user_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Set the opted-out tracking methods. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only manage your own settings")

    changes = payload.get("changes")
    if changes is not None and (
        not isinstance(changes, dict)
        or any(
            k not in api_state.VALID_OPTOUTS or not isinstance(v, bool)
            for k, v in changes.items()
        )
    ):
        raise HTTPException(
            400, "changes must map tracking categories to true or false"
        )
    if changes is None and (
        not isinstance(payload.get("items"), list)
        or any(not isinstance(i, str) for i in payload["items"])
    ):
        raise HTTPException(400, "Provide items or changes")
    pool = api_state._check_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO user_settings(user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                user_id,
            )
            settings = await conn.fetchrow(
                "SELECT game_tracking_enabled, currency_tracking_enabled FROM user_settings WHERE user_id=$1 FOR UPDATE",
                user_id,
            )
            stored = await conn.fetchval(
                "SELECT items FROM opted_out WHERE user_id=$1", user_id
            )
            reactions = await conn.fetchval(
                "SELECT enabled FROM reaction_tracking WHERE user_id=$1", user_id
            )
            assert settings is not None
            items: set[str] = set(stored or ())
            if not settings["game_tracking_enabled"]:
                items.add("games")
            if not settings["currency_tracking_enabled"]:
                items.add("currency")
            if reactions is not True:
                items.add("reactions")
            if changes is not None:
                for key, enabled in changes.items():
                    if enabled:
                        items.discard(key)
                    else:
                        items.add(key)
            else:
                # Legacy dashboards did not expose snipe; only explicit patches may enable it.
                items = (items - (api_state.VALID_OPTOUTS - {"snipe"})) | set(
                    payload["items"]
                ) & api_state.VALID_OPTOUTS
            ordinary = sorted(items - {"games", "currency", "reactions"})
            games_enabled = "games" not in items
            currency_enabled = "currency" not in items
            reactions_enabled = "reactions" not in items
            await conn.execute(
                "INSERT INTO opted_out(user_id, items) VALUES ($1,$2) ON CONFLICT(user_id) DO UPDATE SET items=$2",
                user_id,
                ordinary,
            )
            await conn.execute(
                "UPDATE user_settings SET game_tracking_enabled=$2, currency_tracking_enabled=$3 WHERE user_id=$1",
                user_id,
                games_enabled,
                currency_enabled,
            )
            await conn.execute(
                "INSERT INTO reaction_tracking(user_id, enabled) VALUES ($1,$2) ON CONFLICT(user_id) DO UPDATE SET enabled=$2, updated_at=now()",
                user_id,
                reactions_enabled,
            )

    if api_state.bot_ref:
        if ordinary:
            api_state.bot_ref.db_cache.opted_out[user_id] = ordinary
        else:
            api_state.bot_ref.db_cache.opted_out.pop(user_id, None)
        api_state.bot_ref.db_cache.set_game_tracking_enabled(user_id, games_enabled)
        api_state.bot_ref.db_cache.set_currency_tracking_enabled(
            user_id, currency_enabled
        )
        if reactions_enabled:
            api_state.bot_ref.db_cache.enable_reaction_tracking(user_id)
        else:
            api_state.bot_ref.db_cache.disable_reaction_tracking(user_id)

    return {"items": sorted(items)}


@router.get("/user/{user_id}/privacy-settings")
async def get_user_privacy_settings(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get the authenticated user's global tracking and history settings."""
    await api_auth._require_self(user_id, authorization, session_id)
    row = await api_state._check_pool().fetchrow(
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


@router.post("/user/{user_id}/privacy-settings")
async def set_user_privacy_settings(
    user_id: int,
    payload: dict = Body(...),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Update global tracking and saved-history visibility without deleting data."""
    await api_auth._require_self(user_id, authorization, session_id)
    fields = ("tracking_enabled", "history_public", "game_history_public")
    if any(key in payload and not isinstance(payload[key], bool) for key in fields):
        raise HTTPException(400, "Privacy settings must be booleans")
    row = await api_state._check_pool().fetchrow(
        """
        INSERT INTO user_settings(user_id, tracking_enabled, history_public, game_history_public, tracking_consent)
        VALUES ($1, COALESCE($2, true), COALESCE($3, false), COALESCE($4, true), COALESCE($3, false) OR COALESCE($4, false))
        ON CONFLICT(user_id) DO UPDATE SET
            tracking_enabled = COALESCE($2, user_settings.tracking_enabled),
            history_public = COALESCE($3, user_settings.history_public),
            game_history_public = COALESCE($4, user_settings.game_history_public),
            tracking_consent = user_settings.tracking_consent OR COALESCE($3, false) OR COALESCE($4, false)
        RETURNING tracking_enabled, history_public, game_history_public, tracking_consent
        """,
        user_id,
        *(payload.get(key) for key in fields),
    )
    assert row is not None
    if api_state.bot_ref:
        cache = api_state.bot_ref.db_cache
        if row["tracking_enabled"]:
            cache.tracking_disabled_users.discard(user_id)
        else:
            cache.tracking_disabled_users.add(user_id)
        cache.set_history_public(user_id, row["history_public"])
        cache.set_game_history_public(user_id, row["game_history_public"])
        cache.set_tracking_consent(user_id, row["tracking_consent"])
    return {key: row[key] for key in fields}


async def _refresh_urls(urls: list[str]) -> list[str]:
    """Call Discord's refresh-urls endpoint to get fresh CDN links."""
    if not urls or not api_state.bot_ref:
        return urls
    clean = list({u.split("?")[0] for u in urls if u})
    if not clean:
        return urls
    mapping: dict[str, str] = {}
    BATCH_SIZE = 50
    for i in range(0, len(clean), BATCH_SIZE):
        batch = clean[i : i + BATCH_SIZE]
        try:
            req = await api_state.bot_ref.http.request(
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


@router.get("/guild/{guild_id}/icons")
async def get_guild_icons(
    guild_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(80, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get guild icon history."""
    await api_auth._require_guild_manager(guild_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/guild/{guild_id}/names")
async def get_guild_names(
    guild_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(80, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get guild name history."""
    await api_auth._require_guild_manager(guild_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/user/{user_id}")
async def get_user_data(
    user_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/user/{user_id}/xp")
async def get_user_xp(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get XP and message count for a user. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own XP")
    pool = api_state._check_pool()
    row = await pool.fetchrow(
        "SELECT messages, xp FROM message_xp WHERE user_id = $1", user_id
    )
    if not row:
        return {"messages": 0, "xp": 0}
    return {"messages": row["messages"], "xp": row["xp"]}


@router.get("/usernames/{user_id}")
async def get_usernames(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/display-names/{user_id}")
async def get_display_names(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/discrims/{user_id}")
async def get_discrims(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _history_visible_to(user_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/server-tags/{user_id}")
async def get_server_tags(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get server-tag history when the user permits public history lookup."""
    await _history_visible_to(user_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/statuses/{user_id}")
async def get_status_history(
    user_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=100),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get presence history when the user permits public history lookup."""
    await _history_visible_to(user_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.delete("/user/{user_id}")
async def delete_user_data(
    user_id: int,
    table: str = Query(None),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    me = await api_auth._verify_token(authorization, session_id)

    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only delete your own data")
    pool = api_state._check_pool()
    if table is None:
        if api_state.bot_ref is None:
            raise HTTPException(503, "Bot not ready")
        try:
            deleted = await delete_account(api_state.bot_ref, user_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
    else:
        db_table = api_state.TABLE_MAP.get(table)
        if not db_table or table in api_state.GUILD_TABLES:
            raise HTTPException(400, "Invalid user history table")
        async with deletion(pool, user_id) as conn:
            if db_table == "user_badges":
                await archive_badge_document(conn, user_id)
            result = await conn.execute(f"DELETE FROM {db_table} WHERE user_id=$1", user_id)
            deleted = int(result.rsplit(" ", 1)[-1])
        if db_table == "user_badges" and api_state.bot_ref is not None:
            await invalidate(api_state.bot_ref, {"scope": "user", "id": user_id})
    return {"user_id": str(user_id), "deleted_rows": deleted, "restore_days": 31}


@router.get("/user/{user_id}/pending-deletions")
async def user_pending_deletions(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own pending deletions")
    return await pending_status(api_state._check_pool(), user_id)


@router.post("/user/{user_id}/restore")
async def restore_user_data(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only restore your own data")
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    result = await restore(api_state._check_pool(), user_id)
    await invalidate(api_state.bot_ref, {"scope": "user", "id": user_id, "full": True, "restored": True})
    return result


@router.get("/resolve")
async def resolve_user(
    q: str = Query(..., min_length=2, max_length=64),
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Resolve a Discord username or ID to a user ID."""
    await api_auth._verify_token(authorization, session_id)
    q = q.strip()
    if q.isdigit():
        return {"user_id": str(q)}
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild_id = int(api_state.bot_ref.config.get("ids", {}).get("guild_id", "0") or "0")
    if not guild_id:
        raise HTTPException(400, "Username lookup not available, use a Discord ID")
    guild = api_state.bot_ref.get_guild(guild_id)
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


@router.delete("/item/{table}/{user_id}")
async def delete_item(
    table: str,
    user_id: int,
    key: str = Query(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Delete a specific logged item. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)

    pool = api_state._check_pool()
    async with deletion(pool, user_id, scope="guild" if table in api_state.GUILD_TABLES else "user") as conn:
        db_table = api_state.TABLE_MAP.get(table)
        if not db_table:
            raise HTTPException(400, f"Invalid table: {table}")
        if table in api_state.GUILD_TABLES:
            if not api_state.bot_ref:
                raise HTTPException(503, "Bot not ready")
            guild = api_state.bot_ref.get_guild(user_id)
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
