# Test doubles supply only the Discord/service fields exercised by each test.
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from extensions.context import Context
from extensions.fun import Fun
from extensions.owner import Owner


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("100", 100),
        ("300k", 300000),
        ("1,000", 1000),
        ("1.1k", 1100),
        ("1 hundred thousand", 100000),
        ("1 mil", 1000000),
        ("$1", 1),
    ],
)
async def test_game_boundary_returns_integer(raw, expected):
    ctx = SimpleNamespace(send=AsyncMock())
    value, stop = await Fun._resolve_coin_amount(
        cast("Fun", SimpleNamespace()), cast("Context", ctx), raw
    )
    assert value == expected
    assert type(value) is int
    assert stop is False
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize(
    "command,method",
    [(Owner.dev_currency_add, "credit"), (Owner.dev_currency_remove, "debit")],
)
@pytest.mark.parametrize(
    "raw,expected", [("100", 100), ("300k", 300000), ("1.1k", 1100)]
)
async def test_owner_amount_and_success_message(command, method, raw, expected):
    mutation = AsyncMock(return_value=SimpleNamespace(balance=500000))
    currency = SimpleNamespace(**{method: mutation})
    cog = SimpleNamespace(bot=SimpleNamespace(currency=currency))
    ctx = SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock())
    user = SimpleNamespace(id=2)
    await command.callback(cog, ctx, user, raw)
    mutation.assert_awaited_once()
    assert mutation.call_args.args[1] == expected
    assert f"**{expected:,} Coins**" in ctx.send.call_args.args[0]


async def test_everything_game_amount_requires_confirmation():
    currency = SimpleNamespace(
        get_wallet=AsyncMock(return_value=SimpleNamespace(balance=300000))
    )
    cog = SimpleNamespace(bot=SimpleNamespace(currency=currency))
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1),
        send=AsyncMock(),
        prompt=AsyncMock(return_value=None),
    )
    value, stop = await Fun._resolve_coin_amount(
        cast("Fun", cog), cast("Context", ctx), "everything"
    )
    assert value is None and stop
    ctx.prompt.assert_awaited_once()
