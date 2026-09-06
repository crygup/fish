"""Helpers for exchanging Mudae kakera gifts for Fishie Coins.

Mudae reports gifts as ordinary messages rather than an interaction payload.
Keep the parser deliberately strict so profile, inventory, and other Mudae
messages cannot be mistaken for a gift.  The listener applies the guild and
recipient restrictions separately before crediting the giver's wallet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SUPPORT_GUILD_ID = 848507662437449750
FISHIE_OWNER_ID = 766953372309127168
KAKERA_PER_COIN = 2

# Mudae has used both its custom ``kakera`` emoji and a literal ``:kakera:``
# token in message content.  Keep the mention and amount anchored to the
# complete message so a quoted or otherwise unrelated sentence cannot award
# currency.  ``<@!id>`` is accepted for nickname mentions as well.
MUDAE_KAKERA_GIFT_RE = re.compile(
    r"^\s*<@!?(?P<giver>\d+)>\s+just\s+gifted\s+"
    r"\*\*(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+))\*\*\s*"
    r"(?:<a?:kakera:\d+>|:kakera:)\s*to\s+"
    r"<@!?(?P<receiver>\d+)>\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MudaeKakeraGift:
    """One validated Mudae gift message."""

    giver_id: int
    receiver_id: int
    kakera: int

    @property
    def coins(self) -> int:
        """Return whole Fishie Coins for the two-kakera exchange rate."""

        return self.kakera // KAKERA_PER_COIN


def parse_mudae_kakera_gift(content: str | None) -> MudaeKakeraGift | None:
    """Parse a Mudae ``just gifted`` message, if it is valid.

    The parser does not enforce the support guild, Mudae author, or Fishie
    owner recipient; those checks belong to the event listener and are kept
    separate so this helper can be tested with raw message fixtures.
    """

    match = MUDAE_KAKERA_GIFT_RE.fullmatch(str(content or ""))
    if match is None:
        return None
    try:
        giver_id = int(match.group("giver"))
        receiver_id = int(match.group("receiver"))
        kakera = int(match.group("amount").replace(",", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    if giver_id <= 0 or receiver_id <= 0 or kakera <= 0:
        return None
    return MudaeKakeraGift(giver_id, receiver_id, kakera)


__all__ = [
    "FISHIE_OWNER_ID",
    "KAKERA_PER_COIN",
    "MUDAE_KAKERA_GIFT_RE",
    "MudaeKakeraGift",
    "SUPPORT_GUILD_ID",
    "parse_mudae_kakera_gift",
]
