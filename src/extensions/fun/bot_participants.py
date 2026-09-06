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
    """Generate wagers capped by the highest human bid in the lobby.

    The cap scales down as more humans join: bots may bid up to 80% of the
    highest human bid with one human, 60% with two, 40% with three, 30% with
    four, and 10% with five or more.  The player count is the number of human
    participants, so adding virtual opponents does not change the cap.

    The existing lower-bound behavior is retained whenever it fits below the
    scaled cap: a solo lobby starts at ``minimum`` and a multi-human lobby
    starts at its lowest human bid, clamped to ``minimum``.  When the scaled
    cap is below that lower bound, the cap wins.  This is important for large
    human wagers: the percentage cap must remain an actual cap rather than
    being defeated by the lowest human bid.  The fallback range only applies
    defensively when a caller has no humans (the current games always require
    at least one).
    """

    if count <= 0:
        return []
    minimum = max(0, int(minimum))
    bids = [max(0, int(bid)) for bid in human_bids]
    if bids:
        highest_bid = max(minimum, max(bids))
        low = minimum if len(bids) == 1 else max(minimum, min(bids))
        high = _bot_wager_cap(highest_bid, len(bids), minimum)
        low = min(low, high)
    else:
        low = minimum
        high = max(minimum, int(fallback_maximum))
    return [int(rng.randint(low, high)) for _ in range(count)]


def _bot_wager_cap(highest_bid: int, player_count: int, minimum: int) -> int:
    """Return the inclusive per-bot cap for a human-player count."""

    if player_count <= 1:
        percentage = 80
    elif player_count == 2:
        percentage = 60
    elif player_count == 3:
        percentage = 40
    elif player_count == 4:
        percentage = 30
    else:
        percentage = 10
    return max(minimum, highest_bid * percentage // 100)
