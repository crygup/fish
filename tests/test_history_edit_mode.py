from types import SimpleNamespace
from typing import Any, cast

import pytest
from discord.ext import commands

from extensions.logging.commands import Commands as LoggingCommands


@pytest.mark.asyncio
async def test_avatar_edit_argument_defaults_to_command_author() -> None:
    author = SimpleNamespace(id=1)
    ctx = cast(Any, SimpleNamespace(author=author))

    user, edit = await LoggingCommands._avatar_list_arguments(ctx, ("edit",))

    assert user is author
    assert edit is True


@pytest.mark.asyncio
async def test_avatar_edit_rejects_another_users_history() -> None:
    cog = object.__new__(LoggingCommands)
    cast(Any, cog).ensure_history_visible = lambda _ctx, _user: None
    ctx = cast(Any, SimpleNamespace(author=SimpleNamespace(id=1)))
    target = cast(Any, SimpleNamespace(id=2))

    with pytest.raises(commands.BadArgument, match="only edit your own"):
        await cog.avatars_func(ctx, target, edit=True)


@pytest.mark.asyncio
async def test_icon_edit_requires_manage_server_in_target_guild() -> None:
    cog = object.__new__(LoggingCommands)
    ctx = cast(Any, SimpleNamespace(author=SimpleNamespace(id=1)))
    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_guild=False),
    )
    guild = cast(Any, SimpleNamespace(get_member=lambda _user_id: member))

    with pytest.raises(commands.MissingPermissions) as exc_info:
        await cog.icons_func(ctx, guild, edit=True)

    assert exc_info.value.missing_permissions == ["manage_guild"]
