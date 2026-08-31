"""Helpers for filling multiplayer minigame lobbies with virtual players.

The games deliberately share the same wager and name rules so a bot-filled
lobby feels consistent across Race, Lucky Roll, and future multiplayer games.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from typing import Any

# Names are intentionally kept as display strings rather than Discord users:
# these are virtual opponents, not impersonation of a real member.  The
# explicit ``Bot ...`` entries preserve the old fallback style while giving
# the lobby more variety.
BOT_NAME_POOL: tuple[str, ...] = (
    "Subaru",
    "Choso",
    "Chaewon",
    "PGE",
    "Lenny",
    "Emilia",
    "Fluttershy",
    "Joestar Jonathan",
    "Brando Dio",
    "Joestar Joseph",
    "Shigeo",
    "Kobayashi",
    "Miwa",
    "Mahito",
    "Levi",
    "Ed",
    "Spike",
    "Yuuta",
    "Mitsuki",
    "Aya",
    "Reigen",
    "Ryou",
    "Nijika",
    "Sukuna",
    "Gojo",
    "Geto",
    "Nanami",
    "Trung",
    "Fishie",
    "Dimple",
    "Victor",
    "Bebop",
    "Celeste",
    "Jet",
    "Vyse",
    "Viper",
    "shigechi",
    "Saitama",
    "Genos",
    "Tatsumaki",
    "King",
    "Garou",
    "Rem",
    "Ram",
    "Uraraka",
    "Toga",
    "Anakin Skywalker",
    "Darth Vader",
    "All Might",
    "Deku",
    "Kurisu",
    "Mayuri",
    "Pikachu",
    "Hatsune Miku",
    "Luffy",
    "Bladee",
    "Ecco2k",
    "PinkPantheress",
    "Obi-Wan Kenobi",
    "Daniel",
    "Will A. Zeppeli",
    "OhnePixel",
    "Hakari",
    "Markiplier",
    "Jacksepticeye",
    "Daniel J. D'Arby",
    "Oswald",
    "Morty",
    "Rick",
    "Gucci Morty",
    "Pickle Rick",
    "Winston",
    "Wrecking Ball",
    "Tracer",
    "Junkrat",
    "Mei",
    "Proper",
    "Hawk",
    "Sangha",
    "Bot 1",
    "Bot 2",
    "Bot 3",
    "Bot 4",
    "Bot 5",
    "Bot 6",
    "Bot 7",
    "Bot 8",
    "Bot 9",
    "Bot 10",
    "Bot 67",
    "Bot 100",
    "Bot 333",
    "Bot 69",
    "Bot 420",
    "Bot 117",
    "Bot 112",
    "Bot 41",
    "Bot 61",
    "Bot X",
    "Bot 1000",
)


def _member_display_name(member: object) -> str:
    return str(
        getattr(member, "display_name", None) or getattr(member, "name", None) or ""
    ).strip()


def _server_bot_names(guild: object | None) -> list[str]:
    """Return names of cached bot members without requiring an API request."""

    if guild is None:
        return []
    try:
        members = getattr(guild, "members", ())
    except Exception:
        return []
    result: list[str] = []
    for member in members or ():
        if not bool(getattr(member, "bot", False)):
            continue
        name = _member_display_name(member)
        if name:
            result.append(name)
    return result


def bot_names(
    count: int,
    *,
    guild: object | None = None,
    reserved: Iterable[str] = (),
    rng: random.Random | Any = random,
) -> list[str]:
    """Choose unique virtual-player names.

    Cached bot members from the current server are eligible in addition to the
    configured character/name pool.  No member fetch is performed.  Human
    names passed through ``reserved`` are excluded so a virtual opponent is
    never rendered with the same name as a lobby participant.
    """

    if count <= 0:
        return []
    used = {str(name).casefold() for name in reserved if str(name).strip()}
    candidates: list[str] = []
    seen: set[str] = set(used)
    # Prefer the requested pool while still allowing an actual server bot to
    # appear as a virtual opponent.  Shuffling the combined list keeps both
    # sources random without making a network request.
    for name in (*BOT_NAME_POOL, *_server_bot_names(guild)):
        clean = str(name).strip()
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        candidates.append(clean)
    try:
        rng.shuffle(candidates)
    except AttributeError:
        random.shuffle(candidates)

    result = candidates[:count]
    index = 1
    while len(result) < count:
        fallback = f"Bot {index}"
        index += 1
        if fallback.casefold() in seen:
            continue
        seen.add(fallback.casefold())
        result.append(fallback)
    return result


def bot_wagers(
    human_bids: Iterable[int],
    count: int,
    *,
    rng: random.Random | Any = random,
    minimum: int = 50,
    fallback_maximum: int = 100,
) -> list[int]:
    """Generate wagers bounded by the human bids in the lobby.

    With multiple humans, bots use the inclusive lowest-to-highest human bid
    range.  With one human, the range starts at 50 and ends at that player's
    bid.  The fallback range only applies defensively when a caller has no
    humans (the current games always require at least one).
    """

    if count <= 0:
        return []
    bids = [int(bid) for bid in human_bids]
    if len(bids) >= 2:
        low, high = min(bids), max(bids)
    elif len(bids) == 1:
        low, high = minimum, max(minimum, bids[0])
    else:
        low, high = minimum, max(minimum, fallback_maximum)
    low = max(minimum, low)
    high = max(low, high)
    return [int(rng.randint(low, high)) for _ in range(count)]
