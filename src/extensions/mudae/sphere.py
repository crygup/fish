from __future__ import annotations

import asyncio
import math
import random
import re
from collections.abc import Mapping
from functools import lru_cache
from itertools import combinations
from typing import TYPE_CHECKING, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from core import Cog
from utils.emojis import (
    sp,
    spB,
    spB2,
    spG,
    spG2,
    spL2,
    spO,
    spO2,
    spP,
    spP2,
    spR2,
    spT,
    spT2,
    spU,
    spW2,
    spY,
    spY2,
)

if TYPE_CHECKING:
    from extensions.context import Context

TESTING = False  # set to False for production Mudae
MudaeID = 432610292342587392

SPHERE_MAP: dict[int, str] = {
    sp.id: "red",  # type: ignore
    spR2.id: "red",  # type: ignore
    spB.id: "blue",  # type: ignore
    spT.id: "teal",  # type: ignore
    spG.id: "green",  # type: ignore
    spY.id: "yellow",  # type: ignore
    spO.id: "orange",  # type: ignore
    # Alternate emoji pack used by Mudae in some guilds.
    spB2.id: "blue",  # type: ignore
    spT2.id: "teal",  # type: ignore
    spG2.id: "green",  # type: ignore
    spY2.id: "yellow",  # type: ignore
    spO2.id: "orange",  # type: ignore
    # Some Mudae installations use ``spW2`` for the upgraded/red reward
    # sphere in OC as well as OQ.
    spW2.id: "red",  # type: ignore
}

OQ_SPHERE_MAP: dict[int, str] = {
    sp.id: "red",  # type: ignore
    spR2.id: "red",  # type: ignore
    spP.id: "purple",  # type: ignore
    spB.id: "blue",  # type: ignore
    spT.id: "teal",  # type: ignore
    spG.id: "green",  # type: ignore
    spY.id: "yellow",  # type: ignore
    spO.id: "orange",  # type: ignore
    spB2.id: "blue",  # type: ignore
    spT2.id: "teal",  # type: ignore
    spG2.id: "green",  # type: ignore
    spY2.id: "yellow",  # type: ignore
    spO2.id: "orange",  # type: ignore
    spP2.id: "purple",  # type: ignore
    # In OQ, ``spW`` is the red/reward sphere.  It is named differently
    # from the standard red sphere in alternate Mudae emoji packs.
    spW2.id: "red",  # type: ignore
}

# Mudae can install alternate sphere emoji packs.  The alternate custom
# emojis keep the same semantic names but append ``2`` (for example,
# ``spB2`` is still the blue clue).  IDs are guild-specific, so name aliases
# are required in addition to the IDs above.  Keep these maps separate from
# the display emoji objects: the solver only needs to classify the emoji it
# receives and must not try to render a guild's private custom emoji.
_SPHERE_NAME_MAP: dict[str, str] = {
    "sp": "red",
    "spr": "red",
    "spb": "blue",
    "spt": "teal",
    "spg": "green",
    "spy": "yellow",
    "spo": "orange",
    "spp": "purple",
    # Additional sphere types used by the ``$ot`` game.  Mudae can install
    # alternate emoji packs; in those packs the same semantic name is often
    # suffixed with a digit (``spL2``), handled by ``_sphere_color`` below.
    "spw": "rainbow",
    "spl": "light",
    "spd": "dark",
    "spm": "chaos",
    "spc": "chaos",
}
_SPHERE_HIDDEN_NAMES = {"spu"}
_SPHERE_NAME_MAP.update(
    {f"{name}2": color for name, color in tuple(_SPHERE_NAME_MAP.items())}
)
_SPHERE_HIDDEN_NAMES.add("spu2")

# OQ uses the ``spW`` variant for its red/reward sphere.  The same name is
# used by OT for a different rare (rainbow) sphere, so keep the OQ alias
# scoped to the OQ parser instead of changing the shared name map.
_OQ_SPHERE_NAME_MAP = dict(_SPHERE_NAME_MAP)
_OQ_SPHERE_NAME_MAP.update({"spw": "red", "spw2": "red"})

# OC has the same upgraded/red reward variant in some alternate packs.
_OC_SPHERE_NAME_MAP = dict(_SPHERE_NAME_MAP)
_OC_SPHERE_NAME_MAP.update({"spw": "red", "spw2": "red"})

# ``$ot`` may use a different installed emoji pack from ``$oc``/``$oq``.
# Keep the alternate IDs observed in Mudae's payload in this map so a gateway
# event remains classifiable even if Discord omits an emoji name.
OT_SPHERE_MAP: dict[int, str] = {
    **SPHERE_MAP,
    spB2.id: "blue",  # type: ignore
    spO2.id: "orange",  # type: ignore
    spY2.id: "yellow",  # type: ignore
    spL2.id: "light",  # type: ignore
    spG2.id: "green",  # type: ignore
    spT2.id: "teal",  # type: ignore
    spP2.id: "purple",  # type: ignore
    # OT calls this alternate sphere the rainbow/rare reward sphere. Keep
    # this meaning separate from OC/OQ, where the same emoji is red.
    spW2.id: "rainbow",  # type: ignore
}
OT_SPHERE_MAP.update(
    {
        spP.id: "purple",  # type: ignore
    }
)

# The simulator uses the alternate Mudae sphere pack from the supplied
# payload.  Keeping the actual custom emoji objects here prevents a semantic
# colour such as ``light`` from being rendered as an unrelated Unicode circle
# (which the OT parser would correctly reject as an unknown sphere).
OT_SIM_EMOJIS: dict[str, discord.PartialEmoji] = {
    "blue": spB2,
    "orange": spO2,
    "yellow": spY2,
    "light": spL2,
    "green": spG2,
    "teal": spT2,
    "purple": spP2,
}


class UnsupportedSphereEmoji(ValueError):
    """Raised when a Mudae board uses an emoji Fishie cannot classify."""

    def __init__(self, emoji: object):
        self.emoji = _display_sphere_emoji(emoji)
        super().__init__(
            "We don't have this emoji saved: "
            f"{self.emoji}. Please contact the developers in the support "
            "server to have them add it if it's not a custom emoji."
        )


def _sphere_emoji_parts(emoji: object) -> tuple[int | None, str | None, bool]:
    """Return ``(id, name, animated)`` for a raw or discord emoji object."""

    if isinstance(emoji, Mapping):
        raw_id = emoji.get("id")
        raw_name = emoji.get("name")
        animated = bool(emoji.get("animated", False))
    else:
        raw_id = getattr(emoji, "id", None)
        raw_name = getattr(emoji, "name", None)
        animated = bool(getattr(emoji, "animated", False))

    try:
        emoji_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        emoji_id = None
    name = str(raw_name) if raw_name is not None else None
    return emoji_id, name, animated


def _display_sphere_emoji(emoji: object) -> str:
    """Render an unknown emoji for an actionable user-facing error."""

    emoji_id, name, animated = _sphere_emoji_parts(emoji)
    if emoji_id is not None and name:
        prefix = "a" if animated else ""
        return f"<{prefix}:{name}:{emoji_id}>"
    if name:
        return name
    return "(unknown emoji)"


def _component_emoji(emoji: object) -> discord.PartialEmoji | None:
    """Convert a raw component emoji into a reusable custom emoji object."""

    if isinstance(emoji, discord.PartialEmoji):
        return emoji
    if isinstance(emoji, discord.Emoji):
        return discord.PartialEmoji(
            name=emoji.name,
            id=emoji.id,
            animated=emoji.animated,
        )
    emoji_id, name, animated = _sphere_emoji_parts(emoji)
    if emoji_id is None or not name:
        return None
    return discord.PartialEmoji(name=name, id=emoji_id, animated=animated)


def _sphere_color(emoji: object, mapping: Mapping[int, str]) -> tuple[bool, str | None]:
    """Classify a sphere emoji by ID or semantic name.

    The first return value tells callers whether the emoji is known.  A known
    hidden sphere returns ``(True, None)`` and therefore does not become a
    revealed clue.  Unknown emoji are deliberately surfaced instead of
    silently dropping a grid cell, which would make the solver's advice
    unreliable.
    """

    emoji_id, name, _ = _sphere_emoji_parts(emoji)
    if emoji_id is not None:
        color = mapping.get(emoji_id)
        if color is not None:
            return True, color

    normalized_name = name.casefold() if name else None
    if normalized_name in _SPHERE_HIDDEN_NAMES:
        return True, None
    if normalized_name is not None:
        if mapping is OQ_SPHERE_MAP:
            name_map = _OQ_SPHERE_NAME_MAP
        elif mapping is SPHERE_MAP:
            name_map = _OC_SPHERE_NAME_MAP
        else:
            name_map = _SPHERE_NAME_MAP
        color = name_map.get(normalized_name)
        if color is None:
            # Alternate Mudae packs append a numeric suffix to their sphere
            # names (and this has appeared as more than just ``2``).  Strip
            # only trailing digits so unrelated names remain unsupported.
            base_name = re.sub(r"\d+$", "", normalized_name)
            if base_name in _SPHERE_HIDDEN_NAMES:
                return True, None
            color = name_map.get(base_name)
        # OT supports additional rare sphere types that do not have a local
        # emoji constant in ``utils.emojis``.  Its parser passes the dedicated
        # ``OT_SPHERE_MAP`` so those semantic names remain accepted without
        # making OC/OQ silently accept an unsupported colour.
        if color is not None and (
            color in mapping.values() or mapping is OT_SPHERE_MAP
        ):
            return True, color
    return False, None


GRID_SIZE = 5
CENTER = 12
OQ_TARGET_TOTAL = 4
OQ_PURPLES_BEFORE_RED = 3
OQ_MAX_CLICKS = 7
OQ_INITIAL_MOVES = (6, 16, 8, 18)
OQ_CLUE_VALUES = {
    "blue": 0,
    "teal": 1,
    "green": 2,
    "yellow": 3,
    "orange": 4,
}


def _oq_clicks_used(revealed: dict[int, str]) -> int:
    """Count only non-target clicks toward OQ's seven-click limit."""
    return sum(color in OQ_CLUE_VALUES for color in revealed.values())


def _to_rc(idx: int) -> Tuple[int, int]:
    """Convert linear index to (row, col)."""
    return divmod(idx, GRID_SIZE)


def _from_rc(row: int, col: int) -> int:
    """Convert (row, col) to linear index."""
    return row * GRID_SIZE + col


def _oq_neighbors(position: int) -> Tuple[int, ...]:
    row, column = _to_rc(position)
    neighbors = []
    for row_offset in range(-1, 2):
        for column_offset in range(-1, 2):
            if row_offset == 0 and column_offset == 0:
                continue
            neighbor_row = row + row_offset
            neighbor_column = column + column_offset
            if 0 <= neighbor_row < GRID_SIZE and 0 <= neighbor_column < GRID_SIZE:
                neighbors.append(_from_rc(neighbor_row, neighbor_column))
    return tuple(neighbors)


def _same_row(a: int, b: int) -> bool:
    return a // GRID_SIZE == b // GRID_SIZE


def _same_col(a: int, b: int) -> bool:
    return a % GRID_SIZE == b % GRID_SIZE


def _same_diag(a: int, b: int) -> bool:
    ar, ac = _to_rc(a)
    br, bc = _to_rc(b)
    return abs(ar - br) == abs(ac - bc)


def _adjacent(a: int, b: int) -> bool:
    ar, ac = _to_rc(a)
    br, bc = _to_rc(b)
    return max(abs(ar - br), abs(ac - bc)) == 1


OQ_NEIGHBORS = tuple(
    _oq_neighbors(position) for position in range(GRID_SIZE * GRID_SIZE)
)


def _oq_layouts(revealed: dict[int, str]) -> list[frozenset[int]]:
    """Return every four-target layout compatible with the revealed OQ clues."""
    targets = {
        position for position, color in revealed.items() if color in {"purple", "red"}
    }
    if len(targets) > OQ_TARGET_TOTAL:
        return []
    if sum(color == "red" for color in revealed.values()) > 1:
        return []

    remaining = OQ_TARGET_TOTAL - len(targets)
    candidates = [
        position
        for position in range(GRID_SIZE * GRID_SIZE)
        if position not in revealed
    ]
    if remaining > len(candidates):
        return []

    clues = [
        (position, OQ_CLUE_VALUES[color])
        for position, color in revealed.items()
        if color in OQ_CLUE_VALUES
    ]
    layouts: list[frozenset[int]] = []
    for selected in combinations(candidates, remaining):
        layout = frozenset(targets.union(selected))
        if all(
            sum(neighbor in layout for neighbor in OQ_NEIGHBORS[position]) == value
            for position, value in clues
        ):
            layouts.append(layout)
    return layouts


def _oq_outcome(layout: frozenset[int], position: int) -> str | int:
    if position in layout:
        return "target"
    return sum(neighbor in layout for neighbor in OQ_NEIGHBORS[position])


def _oq_best_clicks(revealed: dict[int, str]) -> list[int]:
    """Return up to four equally ranked next clicks for an OQ board."""
    if not revealed:
        return list(OQ_INITIAL_MOVES)

    targets = {
        position for position, color in revealed.items() if color in {"purple", "red"}
    }
    purple_count = sum(color == "purple" for color in revealed.values())
    clicks_used = _oq_clicks_used(revealed)
    if (
        len(targets) >= OQ_TARGET_TOTAL
        or "red" in revealed.values()
        or purple_count >= OQ_PURPLES_BEFORE_RED
        or clicks_used >= OQ_MAX_CLICKS
    ):
        return []

    layouts = _oq_layouts(revealed)
    if not layouts:
        return []

    unknown = [
        position
        for position in range(GRID_SIZE * GRID_SIZE)
        if position not in revealed
    ]
    frequencies = {
        position: sum(position in layout for layout in layouts) for position in unknown
    }
    guaranteed = [
        position
        for position, frequency in frequencies.items()
        if frequency == len(layouts)
    ]
    if guaranteed:
        return sorted(
            guaranteed,
            key=lambda position: (-len(OQ_NEIGHBORS[position]), position),
        )[:4]

    ranked: list[Tuple[int, float, float, float]] = []
    for position in unknown:
        outcomes: dict[str | int, int] = {}
        for layout in layouts:
            outcome = _oq_outcome(layout, position)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

        entropy = 0.0
        for count in outcomes.values():
            probability = count / len(layouts)
            entropy -= probability * math.log2(probability)

        target_chance = frequencies[position] / len(layouts)
        score = entropy + target_chance * 1.5
        ranked.append((position, score, target_chance, entropy))

    best_score = max(score for _, score, _, _ in ranked)
    tied = [
        entry
        for entry in ranked
        if math.isclose(entry[1], best_score, rel_tol=0.0, abs_tol=1e-9)
    ]
    best_target_chance = max(target_chance for _, _, target_chance, _ in tied)
    return [
        position
        for position, _, _, _ in sorted(
            (
                entry
                for entry in tied
                if math.isclose(entry[2], best_target_chance, rel_tol=0.0, abs_tol=1e-9)
            ),
            key=lambda entry: (-entry[3], entry[0]),
        )[:4]
    ]


# ``$ot`` (Ouro Trace) places one straight ``ship`` for each non-blue sphere
# colour.  The fixed ships are teal (length four), green and yellow (length
# three), while all remaining colours are length-two ships.  The rest of the
# board is blue.  Enumerating line placements gives us a conservative answer
# to the useful question for a player: which covered cells are guaranteed not
# to be blue (free), and which are guaranteed blue (cost a click).
OT_COMMON_SHIPS: tuple[tuple[str, int], ...] = (
    ("teal", 4),
    ("green", 3),
    ("yellow", 3),
)
OT_BOARD_MASK = (1 << (GRID_SIZE * GRID_SIZE)) - 1
OT_LAYOUT_NODE_LIMIT = 1_000_000
OT_COLOR_COUNT_RE = re.compile(
    r"number\s+of\s+different\s+colors?\s*:\s*" r"(?:\*\*)?(\d+)(?:\*\*)?",
    re.I,
)
OT_INITIAL_SAMPLE_COUNT = 10_000


@lru_cache(maxsize=8)
def _ot_line_masks(length: int) -> tuple[int, ...]:
    """Return horizontal and vertical contiguous masks of ``length``."""

    if length <= 0 or length > GRID_SIZE:
        return ()
    masks: list[int] = []
    for row in range(GRID_SIZE):
        for column in range(GRID_SIZE - length + 1):
            mask = 0
            for offset in range(length):
                mask |= 1 << _from_rc(row, column + offset)
            masks.append(mask)
    for column in range(GRID_SIZE):
        for row in range(GRID_SIZE - length + 1):
            mask = 0
            for offset in range(length):
                mask |= 1 << _from_rc(row + offset, column)
            masks.append(mask)
    return tuple(masks)


def _ot_required_masks(
    revealed_items: tuple[tuple[int, str], ...],
) -> tuple[dict[str, int], int]:
    """Build per-colour required-cell masks and the revealed blue mask."""

    required: dict[str, int] = {}
    blue_mask = 0
    for position, colour in revealed_items:
        if colour == "blue":
            blue_mask |= 1 << position
        else:
            required[colour] = required.get(colour, 0) | (1 << position)
    return required, blue_mask


@lru_cache(maxsize=256)
def _ot_layout_statistics(
    number_colors: int,
    revealed_items: tuple[tuple[int, str], ...],
) -> tuple[int, tuple[int, ...], bool]:
    """Return ``(valid_layouts, blue_counts, complete)`` for an OT board.

    ``blue_counts[position]`` is the number of enumerated valid layouts where
    that position is blue.  Enumeration is intentionally bounded so a board
    with no clues cannot block the bot's event loop indefinitely; callers only
    label cells as guaranteed safe/dangerous when ``complete`` is true.
    """

    if not 6 <= number_colors <= 9:
        return 0, (0,) * (GRID_SIZE * GRID_SIZE), True

    required, blue_mask = _ot_required_masks(revealed_items)
    # ``number_colors`` includes blue and the three fixed ships.  The number
    # of rare ships is therefore N - 4.
    rare_count = number_colors - 4
    common_names = {name for name, _ in OT_COMMON_SHIPS}
    rare_names = sorted(
        colour for colour in required if colour not in common_names and colour != "blue"
    )
    if len(rare_names) > rare_count:
        return 0, (0,) * (GRID_SIZE * GRID_SIZE), True

    all_lines = {
        length: _ot_line_masks(length)
        for length in {length for _, length in OT_COMMON_SHIPS} | {2}
    }
    layouts = 0
    nodes = 0
    blue_counts = [0] * (GRID_SIZE * GRID_SIZE)
    truncated = False

    nonblue_mask = 0
    for mask in required.values():
        nonblue_mask |= mask

    def candidates(colour: str, length: int, occupied: int) -> tuple[int, ...]:
        required_mask = required.get(colour, 0)
        # Every revealed non-blue cell belongs to its own colour's line.  A
        # candidate for another colour must not cross those cells; omitting
        # this constraint created impossible layouts and made the solver mark
        # blue cells as safe (or safe cells as dangerous).
        other_revealed = nonblue_mask ^ required_mask
        return tuple(
            mask
            for mask in all_lines[length]
            if mask & occupied == 0
            and mask & blue_mask == 0
            and mask & other_revealed == 0
            and mask & required_mask == required_mask
        )

    def emit(occupied: int) -> None:
        nonlocal layouts, nodes, truncated
        if nodes >= OT_LAYOUT_NODE_LIMIT:
            truncated = True
            return
        nodes += 1
        layouts += 1
        blue = OT_BOARD_MASK ^ occupied
        for position in range(GRID_SIZE * GRID_SIZE):
            if blue & (1 << position):
                blue_counts[position] += 1

    def choose_unknown_rare(start: int, remaining: int, occupied: int) -> None:
        nonlocal nodes, truncated
        if truncated:
            return
        if remaining == 0:
            emit(occupied)
            return
        masks = all_lines[2]
        # Not enough masks remain to fill the requested number of ships.
        if len(masks) - start < remaining:
            return
        for index in range(start, len(masks)):
            if truncated:
                return
            mask = masks[index]
            if mask & occupied or mask & blue_mask or mask & nonblue_mask:
                continue
            choose_unknown_rare(index + 1, remaining - 1, occupied | mask)

    def choose_known_rare(index: int, occupied: int) -> None:
        if truncated:
            return
        if index >= len(rare_names):
            choose_unknown_rare(0, rare_count - len(rare_names), occupied)
            return
        colour = rare_names[index]
        for mask in candidates(colour, 2, occupied):
            choose_known_rare(index + 1, occupied | mask)
            if truncated:
                return

    def choose_common(index: int, occupied: int) -> None:
        if truncated:
            return
        if index >= len(OT_COMMON_SHIPS):
            choose_known_rare(0, occupied)
            return
        colour, length = OT_COMMON_SHIPS[index]
        for mask in candidates(colour, length, occupied):
            choose_common(index + 1, occupied | mask)
            if truncated:
                return

    choose_common(0, 0)
    return layouts, tuple(blue_counts), not truncated


def _ot_recommendations(
    revealed: dict[int, str], number_colors: int
) -> tuple[set[int], set[int], list[int], dict[int, float], bool]:
    """Return safe cells, dangerous cells, ranked moves and blue probabilities."""

    # With no clues every valid board must be considered.  That search is much
    # larger than the useful incremental searches after the first click, so use
    # a deterministic sample for the initial ranking.  The result is marked
    # incomplete and therefore never labels a sampled cell as guaranteed safe
    # or dangerous.
    if not revealed:
        return _ot_initial_recommendations(number_colors)

    revealed_items = tuple(
        sorted(
            (int(position), str(colour).casefold())
            for position, colour in revealed.items()
        )
    )
    layouts, blue_counts, complete = _ot_layout_statistics(
        number_colors, revealed_items
    )
    unknown = set(range(GRID_SIZE * GRID_SIZE)) - set(revealed)
    if not layouts:
        return set(), set(), [], {}, complete

    probabilities = {position: blue_counts[position] / layouts for position in unknown}
    # A bounded sample is useful for ranking, but not safe enough to claim a
    # cell is guaranteed.  Only classify strict 0/1 probabilities from a full
    # enumeration as green/red.
    safe = (
        {position for position in unknown if blue_counts[position] == 0}
        if complete
        else set()
    )
    danger = (
        {position for position in unknown if blue_counts[position] == layouts}
        if complete
        else set()
    )
    # Never offer a cell that is provably blue as a recommendation.  When all
    # remaining cells are forced blue there simply is no useful click to show.
    candidates = safe if safe else unknown - danger
    ranked = sorted(
        candidates, key=lambda position: (probabilities[position], position)
    )
    return safe, danger, ranked[:4], probabilities, complete


def _ot_color_count(content: str) -> int | None:
    """Extract the OT palette size from Mudae's explanatory message."""

    match = OT_COLOR_COUNT_RE.search(content or "")
    return int(match.group(1)) if match else None


def _ot_random_layout(
    number_colors: int = 6, *, rng: random.Random | None = None
) -> dict[int, str]:
    """Generate a valid OT board for ``simot``."""

    number_colors = min(9, max(6, int(number_colors)))
    chooser = rng.choice if rng is not None else random.choice
    rare_names = ("orange", "light", "purple", "rainbow", "dark", "chaos")
    rare_names = list(rare_names[: number_colors - 4])
    line_specs = list(OT_COMMON_SHIPS) + [(colour, 2) for colour in rare_names]
    lines_by_length = {length: _ot_line_masks(length) for _, length in line_specs}
    for _attempt in range(500):
        occupied = 0
        layout: dict[int, str] = {}
        for colour, length in line_specs:
            available = [
                mask for mask in lines_by_length[length] if not mask & occupied
            ]
            if not available:
                break
            mask = chooser(available)
            occupied |= mask
            for position in range(GRID_SIZE * GRID_SIZE):
                if mask & (1 << position):
                    layout[position] = colour
        else:
            for position in range(GRID_SIZE * GRID_SIZE):
                layout.setdefault(position, "blue")
            return layout
    # The fallback is deterministic and still satisfies the line constraints;
    # this path is practically unreachable with a 5x5 board.
    layout = {}
    occupied = 0
    for colour, length in line_specs:
        for mask in lines_by_length[length]:
            if mask & occupied:
                continue
            occupied |= mask
            for position in range(GRID_SIZE * GRID_SIZE):
                if mask & (1 << position):
                    layout[position] = colour
            break
    for position in range(GRID_SIZE * GRID_SIZE):
        layout.setdefault(position, "blue")
    return layout


@lru_cache(maxsize=8)
def _ot_initial_recommendations(
    number_colors: int,
) -> tuple[set[int], set[int], list[int], dict[int, float], bool]:
    """Return a quick, deterministic first-click ranking.

    Exact enumeration is useful once clues constrain the board, but an empty
    board has hundreds of thousands (or millions) of layouts.  Sampling valid
    layouts keeps the initial command responsive while preserving the
    conservative contract that only exact probabilities are labelled green or
    red.
    """

    if not 6 <= number_colors <= 9:
        return set(), set(), [], {}, True
    rng = random.Random(0x4F54494E ^ number_colors)
    blue_counts = [0] * (GRID_SIZE * GRID_SIZE)
    for _ in range(OT_INITIAL_SAMPLE_COUNT):
        layout = _ot_random_layout(number_colors, rng=rng)
        for position, colour in layout.items():
            if colour == "blue":
                blue_counts[position] += 1
    probabilities = {
        position: blue_counts[position] / OT_INITIAL_SAMPLE_COUNT
        for position in range(GRID_SIZE * GRID_SIZE)
    }
    ranked = sorted(
        range(GRID_SIZE * GRID_SIZE),
        key=lambda position: (probabilities[position], position),
    )
    return set(), set(), ranked[:4], probabilities, False


def _possible_red_positions(revealed: dict[int, str]) -> Set[int]:
    """Given {position: color} for revealed spheres, return the set of
    positions where the red sphere could still be."""
    candidates: Set[int] = set(range(GRID_SIZE * GRID_SIZE))
    candidates.discard(CENTER)

    for pos, color in revealed.items():
        if color == "red":
            return {pos}  # found it
        elif color == "orange":
            candidates &= {p for p in candidates if _adjacent(p, pos)}
        elif color == "yellow":
            candidates &= {p for p in candidates if _same_diag(p, pos)}
        elif color == "green":
            candidates &= {
                p for p in candidates if _same_row(p, pos) or _same_col(p, pos)
            }
        elif color == "teal":
            candidates &= {
                p
                for p in candidates
                if _same_row(p, pos) or _same_col(p, pos) or _same_diag(p, pos)
            }
        elif color == "blue":
            candidates -= {
                p
                for p in candidates
                if _same_row(p, pos) or _same_col(p, pos) or _same_diag(p, pos)
            }

    # Remove already-revealed positions
    candidates -= set(revealed)
    return candidates


def _best_next_click(revealed: dict[int, str]) -> Optional[int]:
    """Pick the unrevealed position that maximally narrows red possibilities,
    or after red is found, maximizes score from remaining high-value spheres."""
    max_counts = {"orange": 2, "yellow": 3, "green": 4}
    remaining: dict[str, int] = {}
    for color, count in max_counts.items():
        remaining[color] = count - sum(1 for c in revealed.values() if c == color)

    unrevealed = [
        p for p in range(GRID_SIZE * GRID_SIZE) if p not in revealed and p != CENTER
    ]
    if not unrevealed:
        return None

    red_pos = next((p for p, c in revealed.items() if c == "red"), None)

    if red_pos is not None:
        # Red found, score unrevealed cells by their likely color value
        # Priority: orange (adjacent) > yellow (diag, not adj) > green (row/col, not diag) > teal (row/col/diag) > blue (none)

        def _score(pos: int) -> int:
            adj = _adjacent(pos, red_pos)
            diag = _same_diag(pos, red_pos)
            rowcol = _same_row(pos, red_pos) or _same_col(pos, red_pos)

            if adj and remaining.get("orange", 0) > 0:
                return 50  # likely orange
            if diag and not adj and remaining.get("yellow", 0) > 0:
                return 40  # likely yellow
            if rowcol and not diag and remaining.get("green", 0) > 0:
                return 30  # likely green
            if rowcol or diag:
                return 20  # teal
            return 10  # blue

        return max(unrevealed, key=_score)

    # Red not yet found, original minimax logic
    if not revealed:
        return 16  # optimal starting move: row 4, column 2
    candidates = _possible_red_positions(revealed)
    if not candidates:
        return None
    if len(candidates) == 1:
        return next(iter(candidates))

    unrevealed_candidates = [p for p in unrevealed if p in candidates]
    if not unrevealed_candidates:
        return None

    possible_colors = ["teal", "blue"]
    for color, count in remaining.items():
        if count > 0:
            possible_colors.append(color)
    if sum(1 for c in revealed.values() if c == "red") == 0:
        possible_colors.append("red")

    best_pos = None
    best_worst = len(candidates)

    for pos in unrevealed_candidates:
        max_remaining = 0
        for test_color in possible_colors:
            test = {**revealed, pos: test_color}
            remaining_count = len(_possible_red_positions(test))
            max_remaining = max(max_remaining, remaining_count)
        if max_remaining < best_worst:
            best_worst = max_remaining
            best_pos = pos

    return best_pos


class SphereView(discord.ui.View):
    """5x5 button grid showing sphere chest state."""

    message: Optional[discord.Message]

    def __init__(
        self,
        ctx: Context,
        revealed: dict[int, str],
        recommendation: Optional[int],
        *,
        unknown_positions: set[int] | None = None,
        disabled: bool = False,
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.revealed = revealed
        self.recommendation = recommendation
        self.unknown_positions = set(unknown_positions or ())
        self.disabled = disabled
        self.message = None
        self._build()

    async def _recommendation_click(self, interaction: discord.Interaction) -> None:
        if self.recommendation is None:
            await interaction.response.defer()
            return
        row, col = _to_rc(self.recommendation)
        await interaction.response.send_message(
            f"Click button at **row {row + 1}, column {col + 1}** "
            "on the Mudae message above.",
            ephemeral=True,
        )

    def _build(self) -> None:
        self.clear_items()
        emoji_map = {
            "red": sp,
            "orange": spO,
            "yellow": spY,
            "green": spG,
            "teal": spT,
            "blue": spB,
        }
        hidden = spU
        for idx in range(GRID_SIZE * GRID_SIZE):
            row = idx // GRID_SIZE
            if idx in self.unknown_positions:
                # Leave an unsupported Mudae emoji blank instead of replacing
                # it with a potentially misleading clue.  Once an unknown is
                # seen the entire recommendation grid is disabled by the
                # caller until the missing emoji is added to our catalogue.
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    label="\u200b",
                    emoji=None,
                    disabled=True,
                    row=row,
                )
            elif idx in self.revealed:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.blurple,
                    emoji=emoji_map[self.revealed[idx]],
                    disabled=True,
                    row=row,
                )
            elif idx == self.recommendation and not self.disabled:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.green,
                    emoji=hidden,
                    disabled=False,
                    row=row,
                )
                btn.callback = self._recommendation_click
            else:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    emoji=hidden,
                    disabled=True,
                    row=row,
                )
                btn.callback = self._recommendation_click
            if not btn.callback:
                btn.callback = self._recommendation_click
            self.add_item(btn)


class SphereQView(discord.ui.View):
    """5x5 OQ grid showing revealed colors and recommended clicks."""

    message: Optional[discord.Message]

    def __init__(
        self,
        ctx: Context,
        revealed: dict[int, str],
        recommendations: list[int],
        *,
        unknown_positions: set[int] | None = None,
        disabled: bool = False,
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.revealed = revealed
        self.recommendations = recommendations
        self.unknown_positions = set(unknown_positions or ())
        self.disabled = disabled
        self.message = None
        self._build()

    async def _send_location(
        self, interaction: discord.Interaction, position: int
    ) -> None:
        row, column = _to_rc(position)
        await interaction.response.send_message(
            f"Click button at **row {row + 1}, column {column + 1}** "
            "on the Mudae message above.",
            ephemeral=True,
        )

    def _recommendation_callback(self, position: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self._send_location(interaction, position)

        return callback

    def _build(self) -> None:
        self.clear_items()
        emoji_map = {
            "red": sp,
            "purple": spP,
            "orange": spO,
            "yellow": spY,
            "green": spG,
            "teal": spT,
            "blue": spB,
        }
        recommended = set(self.recommendations)
        for position in range(GRID_SIZE * GRID_SIZE):
            row = position // GRID_SIZE
            if position in self.unknown_positions:
                # Do not render an emoji we cannot classify.  The disabled
                # blank button keeps the 5x5 board aligned while the error is
                # shown on the solver message.
                button = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    label="\u200b",
                    emoji=None,
                    disabled=True,
                    row=row,
                )
            elif position in self.revealed:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.blurple,
                    emoji=emoji_map[self.revealed[position]],
                    disabled=True,
                    row=row,
                )
            elif position in recommended and not self.disabled:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.green,
                    emoji=spU,
                    disabled=False,
                    row=row,
                )
                button.callback = self._recommendation_callback(position)
            else:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    emoji=spU,
                    disabled=True,
                    row=row,
                )
            self.add_item(button)


class SphereOTView(discord.ui.View):
    """5x5 OT board with conservative safe/danger recommendations."""

    message: Optional[discord.Message]

    def __init__(
        self,
        ctx: Context,
        revealed: dict[int, str],
        safe_positions: set[int],
        danger_positions: set[int],
        recommendations: list[int],
        *,
        unknown_positions: set[int] | None = None,
        disabled: bool = False,
        emoji_overrides: Mapping[int, object] | None = None,
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.revealed = revealed
        self.safe_positions = set(safe_positions)
        self.danger_positions = set(danger_positions)
        self.recommendations = list(recommendations)
        self.unknown_positions = set(unknown_positions or ())
        self.disabled = disabled
        self.emoji_overrides = {
            int(position): partial
            for position, emoji in (emoji_overrides or {}).items()
            if (partial := _component_emoji(emoji)) is not None
        }
        self.message = None
        self._build()

    async def _send_location(
        self, interaction: discord.Interaction, position: int
    ) -> None:
        row, column = _to_rc(position)
        await interaction.response.send_message(
            f"Click button at **row {row + 1}, column {column + 1}** "
            "on the Mudae message above.",
            ephemeral=True,
        )

    def _callback(self, position: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self._send_location(interaction, position)

        return callback

    @staticmethod
    def _emoji_for_colour(colour: str):
        # Most installations use the IDs already present in utils.emojis.  A
        # Light sphere ID is included for the alternate pack in the supplied
        # OT payload.  The helper deliberately has no Unicode fallback: OT
        # boards use Mudae's custom sphere artwork, and a plain Unicode circle
        # would misrepresent a clue.  Guild-specific variants are supplied via
        # ``emoji_overrides`` when parsing the source message.
        emoji = {
            # The OT helper follows the alternate pack used by Mudae in the
            # supplied board.  Using those exact IDs also makes ``simot``
            # behave like a real Mudae game instead of mixing in Unicode.
            "orange": OT_SIM_EMOJIS["orange"],
            "yellow": OT_SIM_EMOJIS["yellow"],
            "green": OT_SIM_EMOJIS["green"],
            "teal": OT_SIM_EMOJIS["teal"],
            "blue": OT_SIM_EMOJIS["blue"],
            "light": OT_SIM_EMOJIS["light"],
            # These variants are present in other Mudae installations where
            # the source message carries a raw emoji override.  The base
            # constants remain useful for tests and older boards.
            "red": sp,
            "purple": spP,
        }.get(colour)
        return emoji

    def _build(self) -> None:
        self.clear_items()
        recommended = set(self.recommendations)
        # Keep every green recommendation actionable.  Using ``or`` here
        # discarded ranked recommendations whenever an exact-safe set was
        # present, leaving some green buttons disabled by the final guard.
        clickable = self.safe_positions | recommended
        for position in range(GRID_SIZE * GRID_SIZE):
            row = position // GRID_SIZE
            if position in self.unknown_positions:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    label="\u200b",
                    emoji=None,
                    disabled=True,
                    row=row,
                )
            elif position in self.revealed:
                emoji = self.emoji_overrides.get(position) or self._emoji_for_colour(
                    self.revealed[position]
                )
                if emoji is None:
                    # The parser can classify a rare sphere by its semantic
                    # name even when no local constant exists.  Preserve the
                    # board shape without introducing a non-Mudae emoji;
                    # callers normally provide the raw emoji override.
                    button = discord.ui.Button(
                        style=discord.ButtonStyle.grey,
                        label="\u200b",
                        disabled=True,
                        row=row,
                    )
                    self.add_item(button)
                    continue
                button = discord.ui.Button(
                    style=discord.ButtonStyle.blurple,
                    emoji=emoji,
                    disabled=True,
                    row=row,
                )
            elif position in self.danger_positions:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.red,
                    emoji=spU,
                    disabled=True,
                    row=row,
                )
            elif position in self.safe_positions:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.green,
                    emoji=spU,
                    disabled=self.disabled,
                    row=row,
                )
                if not self.disabled:
                    button.callback = self._callback(position)
            elif position in recommended:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.green,
                    emoji=spU,
                    disabled=self.disabled,
                    row=row,
                )
                if not self.disabled:
                    button.callback = self._callback(position)
            else:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.grey,
                    emoji=spU,
                    disabled=True,
                    row=row,
                )
            # If there are no exact safe cells, the ranked recommendations are
            # the only useful actions.  Keep all other cells visibly neutral.
            if position not in clickable and position not in self.revealed:
                button.disabled = True
            self.add_item(button)


OC_TEXT = "You can click **5** times on the buttons below"
OQ_TEXT = "You can click **7** times on the buttons below"
OT_TEXT = "You can click **4** times on the buttons below"


class SphereCog(Cog):
    """Mudae sphere chest solver."""

    def _parse_sphere_components_tolerant(
        self, components: list
    ) -> tuple[Optional[dict[int, str]], dict[int, object]]:
        """Parse an OC gateway update while retaining unknown cell positions."""
        revealed: dict[int, str] = {}
        unknown: dict[int, object] = {}
        idx = 0
        found_any = False
        for action_row in components:
            for child in action_row.get("components", []):
                found_any = True
                emoji = child.get("emoji")
                if emoji:
                    known, color = _sphere_color(emoji, SPHERE_MAP)
                    if not known:
                        unknown[idx] = emoji
                    elif color:
                        revealed[idx] = color
                idx += 1
        return (revealed if found_any else None), unknown

    def _parse_sphere_components(self, components: list) -> Optional[dict[int, str]]:
        """Parse revealed spheres from raw component data (gateway event)."""
        revealed, unknown = self._parse_sphere_components_tolerant(components)
        if unknown:
            raise UnsupportedSphereEmoji(next(iter(unknown.values())))
        return revealed

    def _parse_sphere_message_tolerant(
        self, message: discord.Message
    ) -> tuple[Optional[dict[int, str]], dict[int, object]]:
        """Parse an OC message while retaining unknown cell positions."""
        revealed: dict[int, str] = {}
        unknown: dict[int, object] = {}
        idx = 0
        found_any = False
        for action_row in message.components:
            if not isinstance(action_row, discord.ActionRow):
                continue
            for child in action_row.children:
                if not isinstance(child, discord.Button):
                    continue
                found_any = True
                if child.emoji:
                    known, color = _sphere_color(child.emoji, SPHERE_MAP)
                    if not known:
                        unknown[idx] = child.emoji
                    elif color:
                        revealed[idx] = color
                idx += 1
        return (revealed if found_any else None), unknown

    def _parse_sphere_message(
        self, message: discord.Message
    ) -> Optional[dict[int, str]]:
        """Parse a Mudae sphere chest message from its discord.Message components."""
        revealed, unknown = self._parse_sphere_message_tolerant(message)
        if unknown:
            raise UnsupportedSphereEmoji(next(iter(unknown.values())))
        return revealed

    def _parse_oq_components_tolerant(
        self, components: list
    ) -> tuple[Optional[dict[int, str]], dict[int, object]]:
        """Parse an OQ gateway update while retaining unknown cell positions."""
        revealed: dict[int, str] = {}
        unknown: dict[int, object] = {}
        position = 0
        found_any = False
        for action_row in components:
            for child in action_row.get("components", []):
                found_any = True
                emoji = child.get("emoji")
                if emoji:
                    known, color = _sphere_color(emoji, OQ_SPHERE_MAP)
                    if not known:
                        unknown[position] = emoji
                    elif color:
                        revealed[position] = color
                position += 1
        return (revealed if found_any else None), unknown

    def _parse_oq_components(self, components: list) -> Optional[dict[int, str]]:
        """Parse an OQ board from raw gateway component data."""
        revealed, unknown = self._parse_oq_components_tolerant(components)
        if unknown:
            raise UnsupportedSphereEmoji(next(iter(unknown.values())))
        return revealed

    def _parse_oq_message_tolerant(
        self, message: discord.Message
    ) -> tuple[Optional[dict[int, str]], dict[int, object]]:
        """Parse an OQ message while retaining unknown cell positions."""
        revealed: dict[int, str] = {}
        unknown: dict[int, object] = {}
        position = 0
        found_any = False
        for action_row in message.components:
            if not isinstance(action_row, discord.ActionRow):
                continue
            for child in action_row.children:
                if not isinstance(child, discord.Button):
                    continue
                found_any = True
                if child.emoji:
                    known, color = _sphere_color(child.emoji, OQ_SPHERE_MAP)
                    if not known:
                        unknown[position] = child.emoji
                    elif color:
                        revealed[position] = color
                position += 1
        return (revealed if found_any else None), unknown

    def _parse_oq_message(self, message: discord.Message) -> Optional[dict[int, str]]:
        """Parse an OQ board from a cached Discord message."""
        revealed, unknown = self._parse_oq_message_tolerant(message)
        if unknown:
            raise UnsupportedSphereEmoji(next(iter(unknown.values())))
        return revealed

    def _parse_ot_components_with_emojis(self, components: list) -> tuple[
        Optional[dict[int, str]],
        dict[int, object],
        dict[int, discord.PartialEmoji],
    ]:
        """Parse an OT gateway update and retain source custom emojis."""

        revealed: dict[int, str] = {}
        unknown: dict[int, object] = {}
        emoji_overrides: dict[int, discord.PartialEmoji] = {}
        position = 0
        found_any = False
        for action_row in components:
            for child in action_row.get("components", []):
                found_any = True
                emoji = child.get("emoji")
                if emoji:
                    known, colour = _sphere_color(emoji, OT_SPHERE_MAP)
                    if not known:
                        unknown[position] = emoji
                    elif colour:
                        revealed[position] = colour
                        if (partial := _component_emoji(emoji)) is not None:
                            emoji_overrides[position] = partial
                position += 1
        return (revealed if found_any else None), unknown, emoji_overrides

    def _parse_ot_components_tolerant(
        self, components: list
    ) -> tuple[Optional[dict[int, str]], dict[int, object]]:
        """Parse an OT gateway update while retaining unknown cell positions."""

        revealed, unknown, _emoji_overrides = self._parse_ot_components_with_emojis(
            components
        )
        return revealed, unknown

    def _parse_ot_components(self, components: list) -> Optional[dict[int, str]]:
        """Parse an OT board from raw gateway component data."""

        revealed, unknown = self._parse_ot_components_tolerant(components)
        if unknown:
            raise UnsupportedSphereEmoji(next(iter(unknown.values())))
        return revealed

    def _parse_ot_message_with_emojis(self, message: discord.Message) -> tuple[
        Optional[dict[int, str]],
        dict[int, object],
        dict[int, discord.PartialEmoji],
    ]:
        """Parse a cached OT board and retain source custom emojis."""

        revealed: dict[int, str] = {}
        unknown: dict[int, object] = {}
        emoji_overrides: dict[int, discord.PartialEmoji] = {}
        position = 0
        found_any = False
        for action_row in message.components:
            if not isinstance(action_row, discord.ActionRow):
                continue
            for child in action_row.children:
                if not isinstance(child, discord.Button):
                    continue
                found_any = True
                if child.emoji:
                    known, colour = _sphere_color(child.emoji, OT_SPHERE_MAP)
                    if not known:
                        unknown[position] = child.emoji
                    elif colour:
                        revealed[position] = colour
                        if (partial := _component_emoji(child.emoji)) is not None:
                            emoji_overrides[position] = partial
                position += 1
        return (revealed if found_any else None), unknown, emoji_overrides

    def _parse_ot_message_tolerant(
        self, message: discord.Message
    ) -> tuple[Optional[dict[int, str]], dict[int, object]]:
        """Parse an OT board from a cached Discord message."""

        revealed, unknown, _emoji_overrides = self._parse_ot_message_with_emojis(
            message
        )
        return revealed, unknown

    def _parse_ot_message(self, message: discord.Message) -> Optional[dict[int, str]]:
        """Parse an OT board from a cached Discord message."""

        revealed, unknown = self._parse_ot_message_tolerant(message)
        if unknown:
            raise UnsupportedSphereEmoji(next(iter(unknown.values())))
        return revealed

    @staticmethod
    def _ot_components_finished(components: list) -> bool:
        """Return whether every OT button in a gateway update is disabled."""

        found = False
        for action_row in components:
            for child in action_row.get("components", []):
                found = True
                if not child.get("disabled", False):
                    return False
        return found

    @commands.command(name="ot", aliases=("spheret",))
    @commands.guild_only()
    async def sphere_ot(self, ctx: Context):
        """Show safe and dangerous moves for a Mudae ``$ot`` game."""

        await self._run_sphere_ot(ctx)

    async def _run_sphere_ot(self, ctx: Context) -> None:
        """Find the latest Mudae OT board and display recommendations."""

        # A slash command must be acknowledged before solving a fresh board;
        # the first-click search is intentionally done off the event loop.
        if getattr(ctx, "interaction", None) is not None:
            try:
                await ctx.defer()
            except discord.InteractionResponded:
                pass

        if not self.bot.user:
            raise commands.BadArgument("Bot not fully loaded, please wait.")

        target_ids = {self.bot.user.id, MudaeID}
        mudae_msg: Optional[discord.Message] = None
        colour_count: int | None = None
        async for message in ctx.channel.history(limit=12):
            if message.author.id not in target_ids or not message.components:
                continue
            if OT_TEXT not in (message.content or ""):
                continue
            parsed_count = _ot_color_count(message.content or "")
            if parsed_count is None:
                continue
            mudae_msg = message
            colour_count = parsed_count
            break

        if not mudae_msg or colour_count is None:
            raise commands.BadArgument(
                "No OT sphere game found. Run `$ot` first, then try again."
            )
        if not 6 <= colour_count <= 9:
            raise commands.BadArgument(
                "This OT game uses an unsupported number of sphere colors "
                f"({colour_count}; expected 6–9)."
            )

        revealed, unknown, emoji_overrides = self._parse_ot_message_with_emojis(
            mudae_msg
        )
        if revealed is None:
            raise commands.BadArgument("Could not parse the OT sphere grid.")

        unknown_error = (
            str(UnsupportedSphereEmoji(next(iter(unknown.values()))))
            if unknown
            else None
        )
        await self._show_sphere_ot(
            ctx,
            revealed,
            mudae_msg,
            colour_count,
            unknown_positions=set(unknown),
            unknown_error=unknown_error,
            emoji_overrides=emoji_overrides,
        )

    async def _show_sphere_ot(
        self,
        ctx: Context,
        revealed: dict[int, str],
        mudae_msg: discord.Message,
        colour_count: int,
        *,
        unknown_positions: set[int] | None = None,
        unknown_error: str | None = None,
        emoji_overrides: Mapping[int, object] | None = None,
    ) -> None:
        has_unknown = bool(unknown_positions)
        if has_unknown:
            safe: set[int] = set()
            danger: set[int] = set()
            recommendations: list[int] = []
        else:
            # Layout enumeration is CPU-bound and can involve hundreds of
            # thousands of candidates.  Keep Discord's event loop responsive.
            safe, danger, recommendations, _probabilities, _complete = (
                await asyncio.to_thread(_ot_recommendations, revealed, colour_count)
            )
        finished = all(
            getattr(child, "disabled", False)
            for row in getattr(mudae_msg, "components", ())
            if isinstance(row, discord.ActionRow)
            for child in row.children
            if isinstance(child, discord.Button)
        )
        view = SphereOTView(
            ctx,
            revealed,
            set() if has_unknown else safe,
            set() if has_unknown else danger,
            [] if has_unknown else recommendations,
            unknown_positions=unknown_positions,
            disabled=has_unknown or finished,
            emoji_overrides=emoji_overrides,
        )
        if unknown_error:
            view.message = await ctx.send(content=unknown_error, view=view)
        else:
            view.message = await ctx.send(view=view)
        if has_unknown or finished:
            return

        while True:
            try:
                event = await self.bot.wait_for(
                    "raw_message_edit",
                    check=lambda payload: payload.message_id == mudae_msg.id,
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                break

            components = event.data.get("components", [])
            if not components:
                continue
            new_revealed, unknown, new_emoji_overrides = (
                self._parse_ot_components_with_emojis(components)
            )
            if unknown:
                if new_revealed is not None:
                    view.revealed = new_revealed
                    view.emoji_overrides = {
                        int(position): partial
                        for position, emoji in new_emoji_overrides.items()
                        if (partial := _component_emoji(emoji)) is not None
                    }
                view.unknown_positions = set(unknown)
                view.safe_positions = set()
                view.danger_positions = set()
                view.recommendations = []
                view.disabled = True
                view._build()
                error = str(UnsupportedSphereEmoji(next(iter(unknown.values()))))
                if view.message is not None:
                    try:
                        await view.message.edit(content=error, view=view)
                    except discord.HTTPException:
                        pass
                break
            if new_revealed is None:
                continue

            if new_revealed == view.revealed:
                # The semantic colours can stay the same while Mudae swaps
                # the emoji pack (or sends the first full update after a
                # click).  Keep the source emoji in sync so we never render a
                # guessed Unicode/custom replacement.
                view.emoji_overrides = {
                    int(position): partial
                    for position, emoji in new_emoji_overrides.items()
                    if (partial := _component_emoji(emoji)) is not None
                }
                # Mudae can disable the final board without changing any
                # emojis.  Still update our helper so the controls reflect the
                # completed game instead of waiting for another edit.
                if not view.disabled and self._ot_components_finished(components):
                    view.disabled = True
                    view._build()
                    if view.message is not None:
                        try:
                            await view.message.edit(view=view)
                        except discord.HTTPException:
                            pass
                    break
                continue

            view.revealed = new_revealed
            view.emoji_overrides = {
                int(position): partial
                for position, emoji in new_emoji_overrides.items()
                if (partial := _component_emoji(emoji)) is not None
            }
            safe, danger, recommendations, _probabilities, _complete = (
                await asyncio.to_thread(_ot_recommendations, new_revealed, colour_count)
            )
            view.safe_positions = safe
            view.danger_positions = danger
            view.recommendations = recommendations
            view.disabled = self._ot_components_finished(components)
            view._build()
            if view.message is not None:
                try:
                    await view.message.edit(view=view)
                except discord.HTTPException:
                    break
            if view.disabled:
                break

    @commands.command(name="simot")
    @commands.guild_only()
    async def simot(self, ctx: Context):
        """Generate a random ``$ot`` board for testing the solver."""

        layout = _ot_random_layout()
        view = SimOTView(ctx, layout)
        await ctx.send(
            OT_TEXT
            + "\nNumber of different colors: **6**"
            + "\nRun `fish ot` for recommendations.",
            view=view,
        )

    @commands.command(name="oc", aliases=("sphere",))
    @commands.guild_only()
    async def sphere(self, ctx: Context):
        """Show the best next move for a Mudae sphere chest game."""
        await self._run_sphere(ctx)

    @commands.hybrid_group(name="mudae")
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae(self, ctx: Context):
        """Use Mudae wish tracking, series tools, and sphere solvers."""
        await ctx.send(
            "Choose `oc`, `oq`, `ot`, `wish`, `wishseries`, `wishkakera`, `wishlist`, "
            "`wishserieslist`, `unwish`, `clearwish`, `unwishseries`, "
            "`clearwishseries`, `unwishkakera`, `wishserieska`, `scrapeseries`, "
            "`serieslist`, `wishbundle`, `recent-claimed`, `toggle`, or `copy`."
        )

    @mudae.command(
        name="ot",
        aliases=("spheret",),
        description="Show safe and dangerous moves for a Mudae OT sphere game.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_ot(self, ctx: Context):
        """Show safe and dangerous moves for a Mudae ``$ot`` game."""

        await self._run_sphere_ot(ctx)

    @mudae.command(
        name="oc",
        aliases=("sphere",),
        description="Show the best next move for a Mudae sphere chest game.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_oc(self, ctx: Context):
        """Show the best next move for a Mudae sphere chest game."""
        await self._run_sphere(ctx)

    async def _run_sphere(self, ctx: Context):
        """Show the best next move for a Mudae sphere chest game."""
        if not self.bot.user:
            raise commands.BadArgument("Bot not fully loaded, please wait.")
        target_ids = [self.bot.user.id, MudaeID]
        mudae_msg: Optional[discord.Message] = None

        async for msg in ctx.channel.history(limit=8):
            if msg.author.id not in target_ids or not msg.components:
                continue
            if msg.author.bot and OC_TEXT in msg.content:
                mudae_msg = msg
                break

        if not mudae_msg:
            raise commands.BadArgument(
                "No sphere chest found. Run `$oc` or `fish simsphere` first, then try again."
            )

        revealed, unknown = self._parse_sphere_message_tolerant(mudae_msg)
        if revealed is None:
            raise commands.BadArgument(
                "Could not parse the sphere chest grid from the message."
            )

        unknown_error = (
            str(UnsupportedSphereEmoji(next(iter(unknown.values()))))
            if unknown
            else None
        )
        await self._show_sphere(
            ctx,
            revealed,
            mudae_msg,
            unknown_positions=set(unknown),
            unknown_error=unknown_error,
        )

    async def _show_sphere(
        self,
        ctx: Context,
        revealed: dict[int, str],
        mudae_msg: discord.Message,
        *,
        unknown_positions: set[int] | None = None,
        unknown_error: str | None = None,
    ):

        recommendation = _best_next_click(revealed)
        has_unknown = bool(unknown_positions)
        view = SphereView(
            ctx,
            revealed,
            None if has_unknown else recommendation,
            unknown_positions=unknown_positions,
            disabled=has_unknown,
        )
        if unknown_error:
            view.message = await ctx.send(content=unknown_error, view=view)
        else:
            view.message = await ctx.send(view=view)
        if has_unknown:
            return

        while True:
            try:
                event = await self.bot.wait_for(
                    "raw_message_edit",
                    check=lambda e: e.message_id == mudae_msg.id,
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                break

            components = event.data.get("components", [])
            if not components:
                continue
            new_revealed, unknown = self._parse_sphere_components_tolerant(components)
            if unknown:
                if new_revealed is not None:
                    view.revealed = new_revealed
                view.unknown_positions = set(unknown)
                view.recommendation = None
                view.disabled = True
                view._build()
                error = str(UnsupportedSphereEmoji(next(iter(unknown.values()))))
                try:
                    await view.message.edit(content=error, view=view)
                except discord.HTTPException:
                    pass
                break
            if new_revealed is None or new_revealed == view.revealed:
                continue

            if len(new_revealed) >= 5:
                view.revealed = new_revealed
                view.recommendation = None
                view._build()
                try:
                    await view.message.edit(view=view)
                except discord.HTTPException:
                    pass
                break

            view.revealed = new_revealed
            view.recommendation = _best_next_click(new_revealed)
            view._build()
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                break

    @commands.command(name="oq", aliases=("sphereq",))
    @commands.guild_only()
    async def sphereq(self, ctx: Context):
        """Show the best next moves for a Mudae OQ sphere game."""
        await self._run_sphereq(ctx)

    @mudae.command(
        name="oq",
        aliases=("sphereq",),
        description="Show the best next moves for a Mudae OQ sphere game.",
    )
    @commands.guild_only()
    @app_commands.allowed_installs(guilds=True)
    @app_commands.allowed_contexts(guilds=True)
    async def mudae_oq(self, ctx: Context):
        """Show the best next moves for a Mudae OQ sphere game."""
        await self._run_sphereq(ctx)

    async def _run_sphereq(self, ctx: Context):
        """Show the best next moves for a Mudae OQ sphere game."""
        if not self.bot.user:
            raise commands.BadArgument("Bot not fully loaded, please wait.")

        target_ids = [self.bot.user.id, MudaeID]
        mudae_msg: Optional[discord.Message] = None
        async for message in ctx.channel.history(limit=8):
            if message.author.id not in target_ids or not message.components:
                continue
            if message.author.bot and OQ_TEXT in message.content:
                mudae_msg = message
                break

        if not mudae_msg:
            raise commands.BadArgument(
                "No OQ sphere game found. Run `$oq` first, then try again."
            )

        revealed, unknown = self._parse_oq_message_tolerant(mudae_msg)
        if revealed is None:
            raise commands.BadArgument("Could not parse the OQ sphere grid.")

        unknown_error = (
            str(UnsupportedSphereEmoji(next(iter(unknown.values()))))
            if unknown
            else None
        )
        await self._show_sphereq(
            ctx,
            revealed,
            mudae_msg,
            unknown_positions=set(unknown),
            unknown_error=unknown_error,
        )

    async def _show_sphereq(
        self,
        ctx: Context,
        revealed: dict[int, str],
        mudae_msg: discord.Message,
        *,
        unknown_positions: set[int] | None = None,
        unknown_error: str | None = None,
    ) -> None:
        recommendations = _oq_best_clicks(revealed)
        has_unknown = bool(unknown_positions)
        view = SphereQView(
            ctx,
            revealed,
            [] if has_unknown else recommendations,
            unknown_positions=unknown_positions,
            disabled=has_unknown,
        )
        if unknown_error:
            view.message = await ctx.send(content=unknown_error, view=view)
        else:
            view.message = await ctx.send(view=view)
        if has_unknown:
            return

        while True:
            try:
                event = await self.bot.wait_for(
                    "raw_message_edit",
                    check=lambda payload: payload.message_id == mudae_msg.id,
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                break

            components = event.data.get("components", [])
            if not components:
                continue
            new_revealed, unknown = self._parse_oq_components_tolerant(components)
            if unknown:
                if new_revealed is not None:
                    view.revealed = new_revealed
                view.unknown_positions = set(unknown)
                view.recommendations = []
                view.disabled = True
                view._build()
                error = str(UnsupportedSphereEmoji(next(iter(unknown.values()))))
                if view.message is not None:
                    try:
                        await view.message.edit(content=error, view=view)
                    except discord.HTTPException:
                        pass
                break
            if new_revealed is None or new_revealed == view.revealed:
                continue

            view.revealed = new_revealed
            view.recommendations = _oq_best_clicks(new_revealed)
            view._build()
            if view.message is None:
                break
            try:
                await view.message.edit(view=view)
            except discord.HTTPException:
                break

            targets = sum(color in {"purple", "red"} for color in new_revealed.values())
            clicks_used = _oq_clicks_used(new_revealed)
            if (
                "red" in new_revealed.values()
                or targets >= OQ_TARGET_TOTAL
                or clicks_used >= OQ_MAX_CLICKS
            ):
                break

    @commands.command(name="simoc")
    async def simoc(self, ctx: Context):
        """Generate a random sphere chest for testing"""
        import random

        candidates = [i for i in range(GRID_SIZE * GRID_SIZE) if i != CENTER]
        red_pos = random.choice(candidates)
        layout: dict[int, str] = {red_pos: "red"}

        orange_candidates = [
            i
            for i in range(GRID_SIZE * GRID_SIZE)
            if i not in layout and _adjacent(i, red_pos)
        ]
        for pos in random.sample(orange_candidates, min(2, len(orange_candidates))):
            layout[pos] = "orange"

        yellow_candidates = [
            i
            for i in range(GRID_SIZE * GRID_SIZE)
            if i not in layout and _same_diag(i, red_pos) and not _adjacent(i, red_pos)
        ]
        for pos in random.sample(yellow_candidates, min(3, len(yellow_candidates))):
            layout[pos] = "yellow"

        green_candidates = [
            i
            for i in range(GRID_SIZE * GRID_SIZE)
            if i not in layout
            and (_same_row(i, red_pos) or _same_col(i, red_pos))
            and not _same_diag(i, red_pos)
        ]
        for pos in random.sample(green_candidates, min(4, len(green_candidates))):
            layout[pos] = "green"

        for i in range(GRID_SIZE * GRID_SIZE):
            if i not in layout:
                if (
                    _same_row(i, red_pos)
                    or _same_col(i, red_pos)
                    or _same_diag(i, red_pos)
                ):
                    layout[i] = "teal"
                else:
                    layout[i] = "blue"

        view = SimSphereView(ctx, layout)
        await ctx.send(OC_TEXT + "\nRun `fish sphere` for help.", view=view)


class SimSphereView(discord.ui.View):
    def __init__(self, ctx: Context, layout: dict[int, str]):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.layout = layout
        self.revealed: dict[int, str] = {}
        self.clicks = 0
        self._emoji_map = {
            "red": sp,
            "orange": spO,
            "yellow": spY,
            "green": spG,
            "teal": spT,
            "blue": spB,
        }
        self._hidden = spU
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for idx in range(GRID_SIZE * GRID_SIZE):
            row = idx // GRID_SIZE
            if idx in self.revealed:
                color = self.revealed[idx]
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    emoji=self._emoji_map[color],
                    disabled=True,
                    row=row,
                )
            else:
                btn = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    emoji=self._hidden,
                    disabled=self.clicks >= 5,
                    row=row,
                )
                btn.callback = self._make_callback(idx)
            self.add_item(btn)

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.defer()
                return
            self.revealed[idx] = self.layout[idx]
            self.clicks += 1
            self._build()
            await interaction.response.edit_message(view=self)

        return callback


class SimOTView(discord.ui.View):
    """Interactive local OT board used by the text-only ``simot`` command."""

    def __init__(self, ctx: Context, layout: dict[int, str]):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.layout = layout
        self.revealed: dict[int, str] = {}
        self.blue_clicks = 0
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for position in range(GRID_SIZE * GRID_SIZE):
            row = position // GRID_SIZE
            if position in self.revealed:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    emoji=SphereOTView._emoji_for_colour(self.revealed[position]),
                    disabled=True,
                    row=row,
                )
            else:
                button = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    emoji=spU,
                    disabled=self.blue_clicks >= 4,
                    row=row,
                )
                button.callback = self._make_callback(position)
            self.add_item(button)

    def _make_callback(self, position: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.defer()
                return
            colour = self.layout[position]
            self.revealed[position] = colour
            if colour == "blue":
                self.blue_clicks += 1
            self._build()
            await interaction.response.edit_message(view=self)

        return callback
