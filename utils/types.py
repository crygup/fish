from typing import ParamSpec, TypedDict, TypeVar

T = TypeVar("T")
P = ParamSpec("P")


class Databases(TypedDict):
    postgre_user: str
    postgre_ip: str
    postgre_pw: str
    postgre_db: str
    testing_postgre_db: str


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
