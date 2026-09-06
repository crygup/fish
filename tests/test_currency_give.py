# Test doubles supply only the Discord/service fields exercised by each test.
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import discord
import pytest
from test_support import invoke_command

from extensions.context import Context
from extensions.currency import Currency


@pytest.mark.parametrize(
    "text,expected",
    [("100", 100), ("300k", 300000), ("1,000", 1000), ("1.1k", 1100), ("$1", 1)],
)
async def test_give_parses_and_reports_the_exact_amount(text, expected):
    old = discord.utils.utcnow() - timedelta(days=100)
    author = SimpleNamespace(id=1, created_at=old)
    recipient = SimpleNamespace(id=2, name="recipient", created_at=old)
    transfer = AsyncMock(
        return_value=SimpleNamespace(sender=SimpleNamespace(balance=700000))
    )
    cog = SimpleNamespace(
        bot=SimpleNamespace(currency=SimpleNamespace(transfer=transfer))
    )
    ctx = SimpleNamespace(
        author=author, message=SimpleNamespace(id=123), send=AsyncMock()
    )

    await invoke_command(
        Currency.give,
        cast("Currency", cog),
        cast("Context", ctx),
        cast("discord.User", recipient),
        amount=text,
    )

    transfer.assert_awaited_once_with(
        1, 2, expected, source="give", reference_key="give:1:2:123"
    )
    assert f"**{expected:,} Coins**" in ctx.send.call_args.args[0]
