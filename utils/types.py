from typing import List, ParamSpec, TypedDict, TypeVar

T = TypeVar("T")
P = ParamSpec("P")


class Webhooks(TypedDict):
    avatars: List[str]
    images: List[str]
    icons: List[str]


class Twitter(TypedDict):
    username: str
    password: str


class Databases(TypedDict):
    postgre_dsn: str
    testing_postgre_dsn: str

    redis_dsn: str
    testing_redis_dsn: str


class Ids(TypedDict):
    owner_id: int
    poketwo_id: int
    mudae_id: int


class Keys(TypedDict):
    fishie_api: str
    lastfm: str
    lastfm_secret: str
    google: List[str]
    google_id: str
    spotify_id: str
    spotify_secret: str


class ConfigTokens(TypedDict):
    bot: str
    testing_bot: str


class Config(TypedDict):
    tokens: ConfigTokens
    keys: Keys
    databases: Databases
    twitter: Twitter
    ids: Ids
    webhooks: Webhooks
