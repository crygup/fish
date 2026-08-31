from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from discord.ext import commands

from core.currency import InsufficientFunds, Wallet
from extensions.owner import Owner


async def test_dev_currency_add_credits_wallet() -> None:
    credit = AsyncMock(return_value=Wallet(user_id=42, balance=325))
    cog = Owner.__new__(Owner)
    cog.bot = cast(Any, SimpleNamespace(currency=SimpleNamespace(credit=credit)))
    ctx = cast(
        Any,
        SimpleNamespace(author=SimpleNamespace(id=7), send=AsyncMock()),
    )
    user = cast(Any, SimpleNamespace(id=42))

    await cast(Any, Owner.dev_currency_add.callback)(cog, ctx, user, 25)

    credit.assert_awaited_once_with(42, 25, "dev_add:7")
    assert "325 Coins" in ctx.send.await_args.args[0]


async def test_dev_currency_remove_rejects_insufficient_balance() -> None:
    debit = AsyncMock(side_effect=InsufficientFunds(10, 25))
    cog = Owner.__new__(Owner)
    cog.bot = cast(Any, SimpleNamespace(currency=SimpleNamespace(debit=debit)))
    ctx = cast(
        Any,
        SimpleNamespace(author=SimpleNamespace(id=7), send=AsyncMock()),
    )
    user = cast(Any, SimpleNamespace(id=42))

    with pytest.raises(commands.BadArgument, match="10 Coins"):
        await cast(Any, Owner.dev_currency_remove.callback)(cog, ctx, user, 25)


def test_dev_currency_command_aliases() -> None:
    assert Owner.dev_currency.aliases == ("coins",)
    assert Owner.dev_currency_add.aliases == ("give",)
    assert Owner.dev_currency_remove.aliases == ("take", "subtract")
