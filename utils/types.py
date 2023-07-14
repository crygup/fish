from typing import List, ParamSpec, TypedDict, TypeVar

T = TypeVar("T")
P = ParamSpec("P")


class Webhooks(TypedDict):
    avatars: List[str]
    images: List[str]
    icons: List[str]


class Databases(TypedDict):
    postgre_dsn: str
    testing_postgre_dsn: str

    redis_dsn: str
    testing_redis_dsn: str


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
    owner_id: int
    tokens: ConfigTokens
    keys: Keys
    databases: Databases
    webhooks: Webhooks
