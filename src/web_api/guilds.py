"""Guilds helpers and endpoints for Fishie."""

from __future__ import annotations

import asyncio
import datetime
import re
from types import SimpleNamespace
from typing import Any, cast

import discord
from discord.ext import commands
from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Header,
    HTTPException,
    Query,
)
from pydantic import BaseModel, ConfigDict, StrictBool

from core.privacy import erase_guild
from core.deletions import deletion, restore, pending_status, invalidate

from . import auth as api_auth
from . import state as api_state

router = APIRouter()


@router.get("/user/{user_id}/guilds")
async def get_user_guilds(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get guilds where the user has Manage Server. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own guilds")
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")

    pool = api_state._check_pool()
    manageable = [
        guild
        for guild in api_state.bot_ref.guilds
        if (member := guild.get_member(user_id))
        and member.guild_permissions.manage_guild
    ]
    opt_out_rows = await pool.fetch(
        "SELECT guild_id, items FROM guild_opted_out WHERE guild_id = ANY($1::BIGINT[])",
        [guild.id for guild in manageable],
    )
    opted_out_by_guild = {row["guild_id"]: row["items"] for row in opt_out_rows}
    pending_rows = await pool.fetch(
        """SELECT DISTINCT d.subject_id FROM privacy_deletions d
           JOIN privacy_deleted_rows r ON r.deletion_id=d.id
             OR EXISTS(SELECT 1 FROM privacy_deletion_holds h
                       WHERE h.row_id=r.id AND h.deletion_id=d.id)
           WHERE d.scope='guild' AND d.subject_id=ANY($1::bigint[])
             AND d.expires_at>now()""",
        [guild.id for guild in manageable],
    )
    pending_guilds = {row["subject_id"] for row in pending_rows}
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
                "pending_deletion": guild.id in pending_guilds,
            }
        )

    guilds.sort(key=lambda g: g["name"].lower())
    return {"guilds": guilds}


@router.get("/guild/{guild_id}/opted-out")
async def get_guild_opted_out(
    guild_id: int,
    authorization: str | None = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get opted-out tracking items for a guild."""
    await api_auth._require_guild_manager(guild_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.get("/user/{user_id}/highlights")
async def get_user_highlights(
    user_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Return a user's highlights for every guild they share with Fishie."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only view your own highlights")
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")

    # Member intent chunks guilds during startup. Avoid issuing one Discord API
    # request per uncached guild from a dashboard request; that pattern does not
    # scale and can exhaust the bot's global rate limit.
    shared_guilds = [
        guild
        for guild in api_state.bot_ref.guilds
        if guild.get_member(user_id) is not None
    ]
    guild_ids = [guild.id for guild in shared_guilds]
    rows: list[Any] = []
    if guild_ids:
        rows = await api_state._check_pool().fetch(
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


@router.post("/user/{user_id}/highlights")
async def set_user_highlights(
    user_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Replace a user's highlights for one shared guild."""
    me = await api_auth._verify_token(authorization, session_id)
    if int(me["id"]) != user_id:
        raise HTTPException(403, "You can only manage your own highlights")
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    raw_guild_id = payload.get("guild_id")
    if not isinstance(raw_guild_id, (str, int)):
        raise HTTPException(400, "guild_id must be a guild ID")
    try:
        guild_id = int(raw_guild_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "guild_id must be a guild ID")
    guild: Any = api_state.bot_ref.get_guild(guild_id)
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

    pool = api_state._check_pool()
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
    tools: Any = api_state.bot_ref.get_cog("Tools")
    if tools is not None and hasattr(tools, "_invalidate_highlights"):
        tools._invalidate_highlights(guild_id)
    return {"guild_id": str(guild_id), "highlights": [word for word, _ in words]}


@router.post("/guild/{guild_id}/opted-out")
async def set_guild_opted_out(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Set opted-out tracking for a guild. Requires OAuth + Manage Server."""
    me = await api_auth._verify_token(authorization, session_id)
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")

    guild = api_state.bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "You need Manage Server permission in this guild")

    changes = payload.get("changes", {})
    if not isinstance(changes, dict) or any(
        key not in api_state.VALID_GUILD_OPTOUTS or not isinstance(value, bool)
        for key, value in changes.items()
    ):
        raise HTTPException(400, "Invalid tracking changes")
    for key in ("tracking_enabled", "history_public"):
        if key in payload and not isinstance(payload[key], bool):
            raise HTTPException(400, "Privacy settings must be booleans")
    if "items" in payload and (
        not isinstance(payload["items"], list)
        or any(not isinstance(item, str) for item in payload["items"])
    ):
        raise HTTPException(400, "items must be a list of categories")
    pool = api_state._check_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO guild_settings(guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                guild_id,
            )
            settings = await conn.fetchrow(
                "SELECT tracking_enabled, history_public FROM guild_settings WHERE guild_id=$1 FOR UPDATE",
                guild_id,
            )
            stored = await conn.fetchval(
                "SELECT items FROM guild_opted_out WHERE guild_id=$1", guild_id
            )
            assert settings is not None
            items: set[str] = set(stored or ())
            if "items" in payload:
                items = (items - api_state.VALID_GUILD_OPTOUTS) | set(
                    payload["items"]
                ) & api_state.VALID_GUILD_OPTOUTS
            for key, enabled in changes.items():
                if enabled:
                    items.discard(key)
                else:
                    items.add(key)
            saved_items = sorted(items)
            tracking_enabled = payload.get(
                "tracking_enabled", settings["tracking_enabled"]
            )
            history_public = payload.get("history_public", settings["history_public"])
            await conn.execute(
                "INSERT INTO guild_opted_out(guild_id,items) VALUES($1,$2) ON CONFLICT(guild_id) DO UPDATE SET items=$2",
                guild_id,
                saved_items,
            )
            await conn.execute(
                "UPDATE guild_settings SET tracking_enabled=$2, history_public=$3 WHERE guild_id=$1",
                guild_id,
                tracking_enabled,
                history_public,
            )

    if api_state.bot_ref:
        if items:
            api_state.bot_ref.db_cache.opted_out[guild_id] = saved_items
        else:
            api_state.bot_ref.db_cache.opted_out.pop(guild_id, None)
        api_state.bot_ref.db_cache.set_guild_tracking_enabled(
            guild_id, tracking_enabled
        )
        api_state.bot_ref.db_cache.set_guild_history_public(guild_id, history_public)

    return {
        "items": saved_items,
        "tracking_enabled": tracking_enabled,
        "history_public": history_public,
    }


class GuildSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_download: int | str | None = None
    poketwo: StrictBool | None = None
    auto_reactions: StrictBool | None = None
    pinboard: int | str | None = None
    honeypot: int | str | None = None


@router.get("/guild/{guild_id}/settings")
async def get_guild_settings(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get guild settings. Requires OAuth."""
    me = await api_auth._verify_token(authorization, session_id)
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild = api_state.bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "Need Manage Server permission")
    pool = api_state._check_pool()
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


@router.post("/guild/{guild_id}/settings")
async def set_guild_settings(
    guild_id: int,
    payload: "GuildSettingsPayload",
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    me = await api_auth._verify_token(authorization, session_id)
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild = api_state.bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "Need Manage Server permission")
    payload_data = payload.model_dump(exclude_unset=True)
    updates = {}
    pool = api_state._check_pool()

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
                placeholders = ", ".join(f"${i + 2}" for i in range(len(gs_updates)))
                set_clause = ", ".join(f"{k} = EXCLUDED.{k}" for k in gs_updates)
                await connection.execute(
                    f"INSERT INTO guild_settings (guild_id, {keys}) VALUES ($1, {placeholders}) ON CONFLICT (guild_id) DO UPDATE SET {set_clause}",
                    guild_id,
                    *list(gs_updates.values()),
                )
                updates.update(gs_updates)

    # Keep the running bot in sync with dashboard changes.  These values are
    # read from db_cache by the event cogs and are otherwise stale until restart.
    cache = api_state.bot_ref.db_cache
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
            api_state.bot_ref.cached_honeypots[guild_id] = updates["honeypot"]
        else:
            api_state.bot_ref.cached_honeypots.pop(guild_id, None)

    return {"settings": updates}


def _dashboard_command_list():
    if not api_state.bot_ref:
        return []
    bot = api_state.bot_ref
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

    for command in api_state.bot_ref.commands:
        add_command(command)
    return sorted(commands_by_name.values(), key=lambda item: item["name"].casefold())


async def _managed_guild(
    guild_id: int, authorization: str | None, session_id: str | None
):
    me = await api_auth._verify_token(authorization, session_id)
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")
    guild = api_state.bot_ref.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    member = guild.get_member(int(me["id"]))
    if not member or not member.guild_permissions.manage_guild:
        raise HTTPException(403, "Need Manage Server permission")
    return guild


@router.get("/guild/{guild_id}/command-disables")
async def get_command_disables(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.post("/guild/{guild_id}/command-disables")
async def set_command_disable(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    if not api_state.bot_ref:
        raise HTTPException(503, "Bot not ready")

    requested = str(payload.get("command", "")).strip()
    command = api_state.bot_ref.get_command(requested.casefold())
    if command is None or command.qualified_name.casefold() != requested.casefold():
        raise HTTPException(400, "Unknown command")
    if api_state.bot_ref._command_disable_excluded(command):
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

    pool = api_state._check_pool()
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
        api_state.bot_ref.db_cache.add_disabled_command(
            guild_id, command_name, channel_id
        )
        disabled = True
    else:
        await pool.execute(
            "DELETE FROM command_disables WHERE guild_id = $1 AND command = $2 AND channel_id = $3",
            guild_id,
            command_name,
            channel_id,
        )
        api_state.bot_ref.db_cache.remove_disabled_command(
            guild_id, command_name, channel_id
        )
        disabled = False
    return {"command": command_name, "channel_id": channel_id, "disabled": disabled}


async def _resolve_guild_text_channel(guild: Any, channel_id: int) -> Any | None:
    """Resolve a guild text channel even when it is not in the local cache."""
    channel = next(
        (item for item in guild.text_channels if item.id == channel_id),
        None,
    )
    if channel is not None:
        return channel
    if api_state.bot_ref is None:
        return None
    try:
        channel = await api_state.bot_ref.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    if (
        isinstance(channel, discord.TextChannel)
        and channel.guild is not None
        and channel.guild.id == guild.id
    ):
        return channel
    return None


@router.get("/guild/{guild_id}/twitch-follows")
async def get_twitch_follows(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    rows = await api_state._check_pool().fetch(
        "SELECT id, channel_name, announce_channel_id, mention_role_id, mention_everyone, broadcaster_id "
        "FROM notify_twitch_follows WHERE guild_id = $1 ORDER BY channel_name",
        guild_id,
    )
    channels = {str(channel.id): channel.name for channel in guild.text_channels}
    return {
        "follows": [
            {
                "channel_name": row["channel_name"],
                "announce_channel_id": (
                    str(row["announce_channel_id"])
                    if row["announce_channel_id"] is not None
                    else None
                ),
                "announce_channel_name": channels.get(
                    str(row["announce_channel_id"]), "Unknown channel"
                ),
                "id": str(row["id"]),
                "mention_role_id": (
                    str(row["mention_role_id"]) if row["mention_role_id"] else None
                ),
                "mention_everyone": row["mention_everyone"],
                "message_template": (
                    "@everyone"
                    if row["mention_everyone"]
                    else (
                        f"<@&{row['mention_role_id']}>"
                        if row["mention_role_id"]
                        else None
                    )
                ),
                "broadcaster_id": row["broadcaster_id"],
            }
            for row in rows
        ],
        "channels": [
            {"id": str(channel.id), "name": channel.name}
            for channel in guild.text_channels
        ],
        "roles": [
            {"id": str(role.id), "name": role.name}
            for role in guild.roles
            if not role.is_default()
        ],
    }


@router.post("/guild/{guild_id}/twitch-follows")
async def set_twitch_follow(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    from extensions.settings.notify import normalize_twitch_channel

    try:
        channel_name = normalize_twitch_channel(str(payload.get("channel_name", "")))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
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
    # The old dashboard field is accepted only as a role mention, not a template.
    mention = str(payload.get("message_template") or "").strip()
    role_id = payload.get("mention_role_id")
    everyone = payload.get("mention_everyone", False)
    if not isinstance(everyone, bool):
        raise HTTPException(400, "mention_everyone must be true or false")
    if mention:
        match = re.fullmatch(r"<@&(\d+)>", mention)
        if mention == "@everyone":
            everyone = True
        elif match:
            role_id = match.group(1)
        else:
            raise HTTPException(
                400, "Use a role mention or @everyone, not a custom message"
            )
    if role_id is not None:
        try:
            role_id = int(role_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid role ID")
        if guild.get_role(role_id) is None or role_id == guild.id or everyone:
            raise HTTPException(400, "Choose one role from this server or @everyone")

    events: Any = api_state.bot_ref.get_cog("Events") if api_state.bot_ref else None
    if events is None or not hasattr(events, "_get_twitch_user"):
        raise HTTPException(503, "Twitch monitoring is unavailable")
    twitch_user = await events._get_twitch_user(channel_name)
    if not twitch_user or not twitch_user.get("id"):
        raise HTTPException(404, "Twitch channel not found")
    broadcaster_id = str(twitch_user["id"])
    pool = api_state._check_pool()
    from extensions.settings.notify import save_twitch_follow

    try:
        follow = await save_twitch_follow(
            pool,
            guild_id=guild_id,
            user_id=None,
            name=channel_name,
            broadcaster_id=broadcaster_id,
            channel_id=announce_channel_id,
            update_existing=True,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if any(
        key in payload
        for key in ("message_template", "mention_role_id", "mention_everyone")
    ):
        await pool.execute(
            "UPDATE notify_twitch_follows SET mention_role_id = $2, mention_everyone = $3, "
            "updated_at = now() WHERE id = $1",
            follow["id"],
            role_id,
            everyone,
        )
    try:
        await events.ensure_twitch_eventsub_subscription(broadcaster_id)
    except Exception as error:
        cast(Any, api_state.bot_ref).logger.warning(
            "Dashboard Twitch subscription failed: %s", error
        )
    return {"channel_name": channel_name}


@router.delete("/guild/{guild_id}/twitch-follows/{channel_name}")
async def delete_twitch_follow(
    guild_id: int,
    channel_name: str,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _managed_guild(guild_id, authorization, session_id)
    channel_name = channel_name.strip().lstrip("@").lower()
    pool = api_state._check_pool()
    broadcaster_id = await pool.fetchval(
        "SELECT broadcaster_id FROM notify_twitch_follows WHERE guild_id = $1 AND lower(btrim(channel_name)) = $2",
        guild_id,
        channel_name,
    )
    result = await pool.execute(
        "DELETE FROM notify_twitch_follows WHERE guild_id = $1 AND lower(btrim(channel_name)) = $2",
        guild_id,
        channel_name,
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Twitch channel is not followed")
    events: Any = api_state.bot_ref.get_cog("Events") if api_state.bot_ref else None
    if broadcaster_id and events is not None:
        await events.remove_twitch_eventsub_subscription(str(broadcaster_id))
    return {"channel_name": channel_name}


@router.get("/guild/{guild_id}/logger")
async def get_logger_settings(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    rows = await api_state._check_pool().fetch(
        "SELECT event, channel_id FROM guild_log_channels WHERE guild_id = $1",
        guild_id,
    )
    channels = {str(channel.id): channel.name for channel in guild.text_channels}
    return {
        "events": api_state.LOGGER_EVENT_LABELS,
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


@router.post("/guild/{guild_id}/logger")
async def set_logger_setting(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    event = str(payload.get("event", "")).strip().casefold()
    if event not in api_state.LOGGER_EVENT_LABELS:
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
    moderation: Any = (
        api_state.bot_ref.get_cog("Moderation") if api_state.bot_ref else None
    )
    if moderation is None or not hasattr(moderation, "_set_logger_channel"):
        raise HTTPException(503, "Logger is unavailable")
    actor_id = None
    try:
        actor_id = int((await api_auth._verify_token(authorization, session_id))["id"])
    except HTTPException:
        pass
    ctx = SimpleNamespace(
        guild=guild, author=SimpleNamespace(id=actor_id), bot=api_state.bot_ref
    )
    await moderation._set_logger_channel(
        ctx, event, channel, announce=False, actor_id=actor_id
    )
    return {"event": event, "channel_id": channel_id}


@router.delete("/guild/{guild_id}/logger/{event}")
async def delete_logger_setting(
    guild_id: int,
    event: str,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _managed_guild(guild_id, authorization, session_id)
    event = event.strip().casefold()
    if event not in api_state.LOGGER_EVENT_LABELS:
        raise HTTPException(400, "Unknown logger event")
    moderation: Any = (
        api_state.bot_ref.get_cog("Moderation") if api_state.bot_ref else None
    )
    pool = api_state._check_pool()
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


@router.get("/guild/{guild_id}/prefixes")
async def get_guild_prefixes(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Get custom prefixes for a guild managed by the authenticated user."""
    await _managed_guild(guild_id, authorization, session_id)
    pool = api_state._check_pool()
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


@router.post("/guild/{guild_id}/prefixes")
async def add_guild_prefix(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Add a custom prefix. Requires OAuth + Manage Server."""
    me, _, _ = await api_auth._require_guild_manager(
        guild_id, authorization, session_id
    )
    prefix = payload.get("prefix", "").strip()
    if not prefix or len(prefix) > 10:
        raise HTTPException(400, "Prefix must be 1-10 characters")
    pool = api_state._check_pool()
    await pool.execute(
        "INSERT INTO guild_prefixes (guild_id, prefix, author_id, time) VALUES ($1, $2, $3, NOW()) ON CONFLICT (guild_id, prefix) DO UPDATE SET author_id = EXCLUDED.author_id, time = NOW()",
        guild_id,
        prefix,
        int(me["id"]),
    )
    if api_state.bot_ref:
        api_state.bot_ref.db_cache.add_prefix(guild_id, prefix)
    return {"prefix": prefix}


@router.delete("/guild/{guild_id}/prefixes")
async def remove_guild_prefix(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Remove a custom prefix. Requires OAuth + Manage Server."""
    await api_auth._require_guild_manager(guild_id, authorization, session_id)
    prefix = payload.get("prefix", "").strip()
    pool = api_state._check_pool()
    await pool.execute(
        "DELETE FROM guild_prefixes WHERE guild_id = $1 AND prefix = $2",
        guild_id,
        prefix,
    )
    if api_state.bot_ref:
        api_state.bot_ref.db_cache.remove_prefix(guild_id, prefix)
    return {"prefix": prefix}


@router.delete("/guild/{guild_id}/data")
async def delete_guild_data(
    guild_id: int,
    table: str | None = Query(None),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    """Delete all tracking data for a guild. Requires OAuth + Manage Server."""
    await api_auth._require_guild_manager(guild_id, authorization, session_id)
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    pool = api_state._check_pool()
    if table is not None:
        if table not in {"guild_icons", "guild_name_logs"}:
            raise HTTPException(400, "Invalid guild history table")
        async with deletion(pool, guild_id, scope="guild") as connection:
            result = await connection.execute(
                f"DELETE FROM {table} WHERE guild_id = $1", guild_id
            )
        return {"deleted": True, "deleted_rows": int(result.split()[-1])}
    auto_download_channel = await pool.fetchval(
        "SELECT auto_download FROM guild_settings WHERE guild_id = $1", guild_id
    )
    async with deletion(pool, guild_id, scope="guild", full=True) as connection:
        deleted = await erase_guild(connection, guild_id)

    # Keep remote webhook resources during the restore window; deleted follow rows stop deliveries.
    cache = api_state.bot_ref.db_cache
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
    api_state.bot_ref.cached_honeypots.pop(guild_id, None)
    return {"deleted": True, "deleted_rows": deleted}


@router.get("/guild/{guild_id}/anime-follows")
async def get_anime_follows(
    guild_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    rows = await api_state._check_pool().fetch(
        "SELECT id, title, anilist_id, announce_channel_id, mention_role_id, mention_everyone, "
        "next_airing_at, release_at, next_episode FROM notify_anime_follows "
        "WHERE guild_id=$1 ORDER BY title",
        guild_id,
    )
    return {
        "follows": [
            {
                key: (
                    str(value)
                    if key
                    in {"id", "anilist_id", "announce_channel_id", "mention_role_id"}
                    and value is not None
                    else value
                )
                for key, value in dict(row).items()
            }
            for row in rows
        ],
        "channels": [
            {"id": str(channel.id), "name": channel.name}
            for channel in guild.text_channels
        ],
        "roles": [
            {"id": str(role.id), "name": role.name}
            for role in guild.roles
            if not role.is_default()
        ],
    }


async def _dashboard_anime(query: str, *, suggest: bool = False):
    from extensions.settings.notify import anilist_notification_schedule, media_title

    settings: Any = api_state.bot_ref.get_cog("Settings") if api_state.bot_ref else None
    if settings is None:
        raise HTTPException(503, "Anime monitoring is unavailable")
    try:
        async with asyncio.timeout(30):
            media = await settings._anilist_lookup(query)
            if not media or str(media.get("type", "ANIME")).upper() != "ANIME":
                raise HTTPException(404, "Anime not found")
            airing, _, release = anilist_notification_schedule(
                media, datetime.datetime.now(datetime.timezone.utc)
            )
            if not airing and not release and suggest:
                media = await settings._find_upcoming_successor(
                    media, datetime.datetime.now(datetime.timezone.utc)
                )
            if not media or not media_title(media):
                raise HTTPException(
                    400, "That anime has no upcoming release or episode."
                )
            return media
    except (commands.BadArgument, TimeoutError) as error:
        raise HTTPException(
            503, "AniList is unavailable. Please try again in 10 minutes."
        ) from error


@router.get("/guild/{guild_id}/anime-search")
async def search_anime_follow(
    guild_id: int,
    q: str = Query(..., min_length=1, max_length=200),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _managed_guild(guild_id, authorization, session_id)
    from extensions.settings.notify import anilist_notification_schedule, media_title

    media = await _dashboard_anime(q, suggest=True)
    airing, episode, release = anilist_notification_schedule(
        media, datetime.datetime.now(datetime.timezone.utc)
    )
    if not airing and not release:
        raise HTTPException(400, "That anime has no upcoming release or episode.")
    return {
        "id": str(media["id"]),
        "title": media_title(media),
        "airing_at": airing or release,
        "episode": episode,
    }


@router.post("/guild/{guild_id}/anime-follows")
async def set_anime_follow(
    guild_id: int,
    payload: dict = Body(...),
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    guild = await _managed_guild(guild_id, authorization, session_id)
    try:
        channel_id = int(payload.get("announce_channel_id") or 0)
        role_id = (
            int(payload["mention_role_id"]) if payload.get("mention_role_id") else None
        )
        follow_id = int(payload["id"]) if payload.get("id") else None
        media_id = int(payload["anilist_id"]) if payload.get("anilist_id") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid channel, role or anime ID")
    if any(
        value is not None and not 0 < value < 2**63
        for value in (channel_id, role_id, follow_id)
    ):
        raise HTTPException(400, "Invalid channel, role or follow ID")
    everyone = payload.get("mention_everyone", False)
    if not isinstance(everyone, bool) or (
        role_id and (everyone or role_id == guild_id or guild.get_role(role_id) is None)
    ):
        raise HTTPException(400, "Choose one role from this server or @everyone")
    if await _resolve_guild_text_channel(guild, channel_id) is None:
        raise HTTPException(400, "Announcement channel must belong to this server")
    pool = api_state._check_pool()
    if follow_id is None:
        if not media_id or not 0 < media_id <= 2147483647:
            raise HTTPException(400, "Choose an anime first")
        from extensions.settings.notify import save_anime_follow

        media = await _dashboard_anime(str(media_id))
        try:
            follow_id = await save_anime_follow(
                pool,
                guild_id=guild_id,
                user_id=None,
                channel_id=channel_id,
                media=media,
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
    result = await pool.execute(
        "UPDATE notify_anime_follows SET announce_channel_id=$3, mention_role_id=$4, "
        "mention_everyone=$5, updated_at=now() WHERE id=$1 AND guild_id=$2",
        follow_id,
        guild_id,
        channel_id,
        role_id,
        everyone,
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Follow not found in this server")
    return {"id": str(follow_id)}


@router.delete("/guild/{guild_id}/anime-follows/{follow_id}")
async def delete_anime_follow(
    guild_id: int,
    follow_id: int,
    authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await _managed_guild(guild_id, authorization, session_id)
    result = await api_state._check_pool().execute(
        "DELETE FROM notify_anime_follows WHERE guild_id=$1 AND id=$2",
        guild_id,
        follow_id,
    )
    if result == "DELETE 0":
        raise HTTPException(404, "Follow not found in this server")
    return {"deleted": True}


@router.get("/guild/{guild_id}/pending-deletions")
async def guild_pending_deletions(
    guild_id: int, authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await api_auth._require_guild_manager(guild_id, authorization, session_id)
    return await pending_status(api_state._check_pool(), guild_id, "guild")


@router.post("/guild/{guild_id}/restore")
async def restore_guild_data(
    guild_id: int, authorization: str = Header(None),
    session_id: str | None = Cookie(None, alias=api_state.SESSION_COOKIE),
):
    await api_auth._require_guild_manager(guild_id, authorization, session_id)
    if api_state.bot_ref is None:
        raise HTTPException(503, "Bot not ready")
    result = await restore(api_state._check_pool(), guild_id, scope="guild")
    await invalidate(api_state.bot_ref, {"scope": "guild", "id": guild_id, "full": True, "restored": True})
    return result
