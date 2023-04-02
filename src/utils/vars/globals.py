from typing import Any, Dict

import aiohttp
import discord

from .emojis import (
    STAFF,
    PARTNER,
    HYPESQUAD,
    BUG_HUNTER,
    BUG_HUNTER_LEVEL_2,
    HS_BALANCE,
    HS_BRAVERY,
    HS_BRILLIANCE,
    EARLY_SUPPORTER,
    VERIFIED_BOT,
    VERIFIED_BOT_DEV,
    CERTIFIED_MODERATOR,
    SYSTEM,
    ACTIVE_DEVELOPER,
)

BURPLE = discord.ButtonStyle.blurple
GREEN = discord.ButtonStyle.green
RED = discord.ButtonStyle.red

initial_extensions = [
    "jishaku",
    "cogs.owner",
    "cogs.context",
    "cogs.events.errors",
    "cogs.help",
    "cogs.tasks",
]

module_extensions = [
    "cogs.discord_",
    "cogs.moderation",
    "cogs.tools",
    "cogs.lastfm",
]

default_headers = {"User-Agent": f"aiohttp/{aiohttp.__version__}; fish_bot"}

emoji_extras = {"BPerms": ["Manage Emojis"], "UPerms": ["Manage Emojis"]}

lastfm_period = {
    "overall": "overall",
    "7day": "weekly",
    "1month": "monthly",
    "3month": "quarterly",
    "6month": "half-yearly",
    "12month": "yearly",
}


USER_FLAGS = {
    "staff": f"{STAFF} Discord Staff",
    "partner": f"{PARTNER} Discord Partner",
    "hypesquad": f"{HYPESQUAD} HypeSquad",
    "bug_hunter": f"{BUG_HUNTER} Bug Hunter",
    "bug_hunter_level_2": f"{BUG_HUNTER_LEVEL_2} Bug Hunter 2",
    "hypesquad_bravery": f"{HS_BRAVERY} HypeSquad Bravery",
    "hypesquad_brilliance": f"{HS_BRILLIANCE} HypeSquad Brilliance",
    "hypesquad_balance": f"{HS_BALANCE}HypeSquad Balance",
    "early_supporter": f"{EARLY_SUPPORTER} Early Supporter",
    "verified_bot_developer": f"{VERIFIED_BOT_DEV} Bot Developer",
    "verified_bot": f"{VERIFIED_BOT} Verified Bot",
    "discord_certified_moderator": f"{CERTIFIED_MODERATOR} Moderator",
    "system": f"{SYSTEM} System",
    "active_developer": f"{ACTIVE_DEVELOPER} Active Developer",
}

anime_query_data = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    description(asHtml: false)
    averageScore
    episodes
    status
    bannerImage
    siteUrl
    source
    chapters
    volumes
    format
    type
    startDate {
      year
      month
      day
    }
    endDate {
      year
      month
      day
    }
    nextAiringEpisode {
      episode
      airingAt
    }
    title {
      romaji
      english
      native
    }
    coverImage {
      extraLarge
      color
    }
    tags {
      name
    }
    genres
    trailer {
      site
      id
    }
  }
}
"""

id_converter = {
    "video": "videoId",
    "channel": "channelId",
    "playlist": "playlistId",
}

link_converter = {
    "video": "watch?v=",
    "channel": "channel/",
    "playlist": "playlist?list=",
}

VALID_EDIT_KWARGS: Dict[str, Any] = {
    "content": None,
    "embeds": [],
    "attachments": [],
    "suppress": False,
    "delete_after": None,
    "allowed_mentions": None,
    "view": None,
}

status_state = {
    0: "Offline",
    1: "Online",
    2: "Busy",
    3: "Away",
    4: "Snooze",
    5: "Looking to trade",
    6: "Looking to play",
}

TATSU_ID: int = 172002275412279296
OWNER_ID: int = 766953372309127168
TABLE_BOOSTER_ID: int = 848529361362747422
TABLE_ID: int = 848507662437449750
