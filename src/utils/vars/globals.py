from typing import Any, Dict

import aiohttp
import discord

from .regexes import (
    INSTAGRAM_RE,
    REDDIT_RE,
    TIKTOK_RE,
    TWITCH_RE,
    TWITTER_RE,
    YOUTUBE_RE,
    YT_CLIP_RE,
    YT_SHORT_RE,
)
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

OsuMods = {
    "DT": "<:doubletime:1047996368528089118>",
    "NM": "",
    "NF": "<:nofail:1047996491731574834>",
    "EZ": "<:easy:1047996366628065301>",
    "TD": "<:target:1047996484563509299>",
    "HD": "<:hidden:1047996494373998602>",
    "HR": "<:hardrock:1047996495519039529>",
    "SD": "<:suddendeath:1047996485960208535>",
    "RX": "<:relax:1047996489131106325>",
    "HT": "<:halftime:1047996364207947847>",
    "NC": "<:nightcore:1047996493107318866>",
    "FL": "<:flashlight:1047996365155872829>",
    "AT": "<:at:1047996488044773396>",
    "SO": "<:spunout:1047996487151398942>",
    "AP": "<:autoplay:1047996370881101947>",
    "PF": "<:perfect:1047996490435543040>",
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
