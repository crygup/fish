from typing import ParamSpec, TypedDict, TypeVar

T = TypeVar("T")
P = ParamSpec("P")


class Character(TypedDict):
    username: str
    password: str


class Databases(TypedDict):
    postgre_dsn: str
    testing_postgre_dsn: str

    redis_dsn: str
    testing_redis_dsn: str


class Keys(TypedDict):
    fishie_api: str
    lastfm: str
    lastfm_secret: str


class ConfigTokens(TypedDict):
    bot: str
    evi: str


class Config(TypedDict):
    tokens: ConfigTokens
    keys: Keys
    databases: Databases
    character: Character
