from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast

import discord

# Test doubles supply only the Discord/service fields exercised by each test.
from extensions.context import Context
from extensions.mudae.recent_claims import (
    RecentClaim,
    RecentClaimsView,
    claim_transition,
    claiming_username,
    parse_mudae_spawn,
)


def _card(*, claimed: bool = False, reactions: list[dict] | None = None) -> dict:
    footer = "Belongs to crygup" if claimed else "Ahri / League of Legends - 345 ka"
    return {
        "embeds": [
            {
                "author": {"name": "Ahri"},
                "description": (
                    "League of Legends\nClaims: #635\n**345**<:kakera:469835869059153940>\n"
                    "React with any emoji to claim!"
                ),
                "footer": {"text": footer},
            }
        ],
        "reactions": reactions or [],
    }


def test_recent_claim_parser_accepts_spawn_and_claim_transition() -> None:
    before = _card()
    after = _card(
        claimed=True,
        reactions=[{"count": 1, "emoji": {"name": "❤️", "id": None}}],
    )

    assert parse_mudae_spawn(before["embeds"][0]) is not None
    assert parse_mudae_spawn(after["embeds"][0], allow_claimed=True) is not None
    assert claiming_username(after["embeds"][0]) == "crygup"
    assert claim_transition(before, after) is not None
    assert (
        claim_transition(after, _card(claimed=True, reactions=[{"count": 2}])) is None
    )


def test_recent_claim_does_not_require_mudaes_default_before_footer() -> None:
    before = _card()
    before["embeds"][0]["footer"] = {"text": "A custom footer"}
    after = _card(
        claimed=True,
        reactions=[{"count": 1, "emoji": {"name": "❤️", "id": None}}],
    )

    assert claim_transition(before, after) is not None


def test_recent_claim_view_has_ten_entry_pages() -> None:
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        bot=SimpleNamespace(embedcolor=discord.Colour.blurple()),
    )
    timestamp = datetime.now(timezone.utc)
    claims = tuple(
        RecentClaim(
            id=index,
            channel_id=456,
            character_name=f"Character {index}",
            claiming_username="tester",
            claiming_user_id=123,
            claimed_at=timestamp,
        )
        for index in range(11)
    )
    view = RecentClaimsView(cast("Context", ctx), claims)

    assert view.page_count == 2
    assert [button.label for button in view._buttons] == ["<", ">"]
