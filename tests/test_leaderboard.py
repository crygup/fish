from types import SimpleNamespace
from typing import Any, cast

from extensions.tools import Tools


def _tools_with_bot(bot: Any) -> Tools:
    tools = object.__new__(Tools)
    tools.bot = bot
    return tools


def test_leaderboard_name_uses_cached_user_without_rest_fetch() -> None:
    cached = SimpleNamespace(name="cached-user")

    async def fetch_user(_user_id: int) -> Any:
        raise AssertionError("leaderboards must not fetch users from Discord")

    bot = SimpleNamespace(
        get_user=lambda _user_id: cached,
        fetch_user=fetch_user,
    )
    tools = _tools_with_bot(bot)

    assert tools._leaderboard_name(cast(Any, SimpleNamespace(guild=None)), 123) == (
        "cached-user"
    )


def test_leaderboard_name_uses_current_guild_member_before_falling_back_to_id() -> None:
    member = SimpleNamespace(name="guild-user")
    guild = SimpleNamespace(get_member=lambda _user_id: member)
    bot = SimpleNamespace(get_user=lambda _user_id: None)
    tools = _tools_with_bot(bot)

    assert tools._leaderboard_name(cast(Any, SimpleNamespace(guild=guild)), 456) == (
        "guild-user"
    )


def test_leaderboard_name_falls_back_to_id_without_network_access() -> None:
    bot = SimpleNamespace(get_user=lambda _user_id: None)
    tools = _tools_with_bot(bot)

    assert tools._leaderboard_name(cast(Any, SimpleNamespace(guild=None)), 789) == "789"
