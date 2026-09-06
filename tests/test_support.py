"""Runtime assertions that also narrow types in UI and mock-based tests."""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from discord.ext import commands

from extensions.context import Context


def not_none[T](value: T | None) -> T:
    assert value is not None
    return value


def require_type[T](value: object, expected: type[T]) -> T:
    assert isinstance(value, expected)
    return value


async def invoke_command(
    command: "commands.Command[Any, ..., Any]",
    cog: object,
    ctx: Context,
    *args: object,
    **kwargs: object,
) -> object:
    """Invoke an unbound Discord command callback with explicit test arguments."""
    callback = cast(Callable[..., Awaitable[object]], command.callback)
    return await callback(cog, ctx, *args, **kwargs)
