# Test doubles supply only the Discord/service fields exercised by each test.
from types import SimpleNamespace
from typing import cast

import discord

from extensions.context import Context
from extensions.fun.pvp import DuelBidView


async def _on_ready(*_args: object) -> None:
    pass


def _view() -> DuelBidView:
    ctx = SimpleNamespace(
        channel=SimpleNamespace(id=123),
        bot=SimpleNamespace(embedcolor=None),
    )
    challenger = SimpleNamespace(id=1, mention="<@1>", name="challenger")
    opponent = SimpleNamespace(id=2, mention="<@2>", name="opponent")
    return DuelBidView(
        cast("Context", ctx),
        cast("discord.User", challenger),
        cast("discord.User", opponent),
        game_name="Test",
        challenger_bid=100,
        on_ready=_on_ready,
    )


async def test_custom_bid_enables_confirmation_without_matching_challenger() -> None:
    view = _view()

    assert view.confirm.disabled
    # The opponent entered a custom amount instead of using Match bid.
    # Different bids are valid; both players still need to confirm.
    assert await view.set_bid_from_command(2, 75)

    assert not view.confirm.disabled
    assert view.bids[1].amount == 100
    assert view.bids[2].amount == 75


async def test_changing_a_bid_resets_both_confirmations() -> None:
    view = _view()
    await view.set_bid_from_command(2, 75)
    view.bids[1].confirmed = True
    view.bids[2].confirmed = True

    assert await view.set_bid_from_command(2, 80)

    assert not view.bids[1].confirmed
    assert not view.bids[2].confirmed
    assert not view.confirm.disabled
