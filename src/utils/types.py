from typing import (
    List,
    NotRequired,
    Optional,
    ParamSpec,
    TypeAlias,
    TypedDict,
    TypeVar,
    Union,
)

import discord

T = TypeVar("T")
P = ParamSpec("P")
EmojiInputType = Union[discord.Emoji, discord.PartialEmoji, str]

AllChannels: TypeAlias = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.CategoryChannel,
    discord.StageChannel,
    discord.ForumChannel,
    discord.Thread,
]
AllMsgbleChannels: TypeAlias = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.StageChannel,
    discord.Thread,
]

DiscordObjects: TypeAlias = Optional[
    Union[
        discord.Message,
        discord.Member,
        discord.User,
        discord.Guild,
        AllChannels,
    ]
]


class Webhooks(TypedDict):
    images: List[str]
    error_logs: str
    messages: str
    phone_logs: str


class Twitter(TypedDict):
    username: str
    password: str


class Databases(TypedDict):
    psql: str
    psql_testing: str


class Ids(TypedDict):
    owner_id: int
    bot_id: int
    # Optional IDs are used when more than one Fishie application shares the
    # same database.  Existing config files can omit them; the runtime falls
    # back to the known application IDs until they are added explicitly.
    new_bot_id: NotRequired[int]
    testing_bot_id: NotRequired[int]
    poketwo_id: int
    mudae_id: int
    join_logs_id: int


class Keys(TypedDict):
    fishie_api: str
    media_api: str
    media_api_owner: str
    client_secret: str
    # OAuth credentials for the replacement/new Fishie application.  This is
    # optional so legacy/testing config files remain valid.
    new_client_secret: NotRequired[str]
    lastfm: str
    lastfm_secret: str
    lastfm_cb: str
    lastfm_cb_secret: str
    twitch_id: str
    twitch_secret: str
    twitch_eventsub_secret: str
    youtube_websub_secret: str
    google: List[str]
    google_id: str
    spotify_id: str
    spotify_secret: str
    anilist_id: str
    anilist_secret: str
    dagpi: str
    roblox: str
    steam: str


class ConfigTokens(TypedDict):
    bot: str
    testing_bot: str
    # Token for the replacement/new Fishie application.  Older deployments
    # may omit it and will fail with a clear startup error only when selected.
    new_bot: NotRequired[str]


class Config(TypedDict):
    tokens: ConfigTokens
    keys: Keys
    databases: Databases
    twitter: Twitter
    ids: Ids
    webhooks: Webhooks
