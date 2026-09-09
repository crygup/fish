"""Shared bot reference, API settings, and bounded process-local caches.

Initialize once from launcher.py. These caches and locks assume the single
bot/API process used by production; they are not shared across workers.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from cachetools import TTLCache
from fastapi import (
    HTTPException,
)

if TYPE_CHECKING:
    from core import Fishie

from extensions.media_effects.commands import (
    PIPELINE_EFFECT_ALIASES,
    PIPELINE_EFFECTS,
)

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


MAX_WEBHOOK_BYTES = 1 * 1024 * 1024


MAX_MEDIA_API_BYTES = 50 * 1024 * 1024


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


_legacy_lastfm_states_used = TTLCache[str, bool](maxsize=20_000, ttl=LASTFM_STATE_TTL)


_twitch_eventsub_replays = TTLCache[str, bool](
    maxsize=20_000, ttl=TWITCH_EVENTSUB_MAX_AGE
)


_twitch_eventsub_replay_lock = asyncio.Lock()


_discord_user_negative_cache = TTLCache[int, bool](maxsize=10_000, ttl=300)


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


VALID_OPTOUTS = {
    "snipe",
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


_stats_lock = asyncio.Lock()


_spotify_cover_cache = TTLCache[tuple[str, str], str](maxsize=1024, ttl=600)


_spotify_cover_negative_cache = TTLCache[tuple[str, str], bool](maxsize=4096, ttl=300)


_spotify_cover_token_lock = asyncio.Lock()


_spotify_cover_semaphore = asyncio.Semaphore(4)


_msg_rate_limit = TTLCache[str, bool](maxsize=10_000, ttl=60)


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
