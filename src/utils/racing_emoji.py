"""Validation and pricing helpers for purchased racing emoji.

The race game stores the rendered emoji selected by each participant, while
the Coins shop needs a deterministic way to decide whether an emoji can be
sold and which price bucket it belongs to.  This module intentionally has no
database or Discord-cog dependencies; purchase/equip code can use the same
pure helpers for validation, shop rendering, and race filtering.

Custom Discord emoji are always in the ``custom`` bucket.  Unicode emoji are
classified from their CLDR name provided by :mod:`emoji`.  The lists below
are deliberately conservative: an unrecognised Unicode emoji is valid and
falls into ``misc`` rather than being rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

import emoji as emoji_lib

RacingEmojiCategory = Literal[
    "custom",
    "sea_animal",
    "human_face",
    "heart",
    "animal",
    "food",
    "misc",
]

RACING_EMOJI_PRICES: Final[dict[RacingEmojiCategory, int]] = {
    "custom": 100_000,
    "sea_animal": 50_000,
    "human_face": 15_000,
    "heart": 20_000,
    "animal": 25_000,
    "food": 15_000,
    "misc": 10_000,
}

RACING_EMOJI_CATEGORY_NAMES: Final[dict[RacingEmojiCategory, str]] = {
    "custom": "Custom",
    "sea_animal": "Sea animals",
    "human_face": "Human/face",
    "heart": "Hearts",
    "animal": "Animals",
    "food": "Food",
    "misc": "Miscellaneous",
}

# Keep this in sync with the default race animals.  A purchased Unicode sea
# animal that is not part of the default list is still useful in a race and is
# handled by the broader animal keyword fallback below.
DEFAULT_RACE_SEA_ANIMALS: Final[frozenset[str]] = frozenset(
    {
        "blowfish",
        "crab",
        "crocodile",
        "dolphin",
        "fish",
        "jellyfish",
        "lobster",
        "octopus",
        "seal",
        "shark",
        "shrimp",
        "squid",
        "tropical_fish",
        "whale",
    }
)

# The CLDR names are stable, but matching words instead of hard-coding every
# code point also covers skin-tone and gender variants where applicable.
SEA_ANIMAL_TERMS: Final[frozenset[str]] = frozenset(
    {
        "blowfish",
        "crab",
        "crocodile",
        "dolphin",
        "fish",
        "jellyfish",
        "lobster",
        "octopus",
        "seal",
        "shark",
        "shrimp",
        "squid",
        "tropical_fish",
        "whale",
        "shell",
        "snail",
        "turtle",
        "otter",
        "beaver",
        "coral",
        "seahorse",
    }
)
ANIMAL_TERMS: Final[frozenset[str]] = frozenset(
    {
        "ant",
        "badger",
        "bat",
        "bear",
        "bee",
        "beetle",
        "bird",
        "boar",
        "bug",
        "butterfly",
        "camel",
        "cat",
        "chicken",
        "cow",
        "deer",
        "dog",
        "dove",
        "duck",
        "eagle",
        "elephant",
        "fox",
        "frog",
        "giraffe",
        "goat",
        "gorilla",
        "hamster",
        "hedgehog",
        "hippo",
        "horse",
        "kangaroo",
        "koala",
        "leopard",
        "lion",
        "lizard",
        "llama",
        "monkey",
        "mouse",
        "panda",
        "parrot",
        "peacock",
        "penguin",
        "pig",
        "rabbit",
        "ram",
        "rat",
        "raccoon",
        "rhino",
        "rooster",
        "scorpion",
        "sheep",
        "skunk",
        "sloth",
        "snake",
        "spider",
        "squirrel",
        "swan",
        "tiger",
        "turkey",
        "unicorn",
        "wolf",
        "zebra",
    }
)
FOOD_TERMS: Final[frozenset[str]] = frozenset(
    {
        "apple",
        "avocado",
        "bacon",
        "banana",
        "beans",
        "beverage",
        "bread",
        "burger",
        "burrito",
        "cake",
        "candy",
        "carrot",
        "cheese",
        "cherries",
        "chestnut",
        "chocolate",
        "coconut",
        "coffee",
        "cookie",
        "corn",
        "cucumber",
        "curry",
        "dumpling",
        "egg",
        "fish_cake",
        "fries",
        "garlic",
        "grapes",
        "hamburger",
        "honey",
        "hotdog",
        "ice_cream",
        "kiwi",
        "lemon",
        "lollipop",
        "melon",
        "milk",
        "mushroom",
        "noodles",
        "olive",
        "onion",
        "orange",
        "peach",
        "peanuts",
        "pear",
        "pepper",
        "pie",
        "pizza",
        "popcorn",
        "potato",
        "ramen",
        "rice",
        "salad",
        "sandwich",
        "shallow_pan_of_food",
        "spaghetti",
        "strawberry",
        "sushi",
        "taco",
        "tea",
        "tomato",
        "tropical_drink",
        "watermelon",
        "wine",
    }
)
HUMAN_FACE_TERMS: Final[frozenset[str]] = frozenset(
    {
        "alien",
        "angel",
        "clown",
        "face",
        "ghost",
        "goblin",
        "man",
        "person",
        "robot",
        "skull",
        "smile",
        "woman",
    }
)

# Emoji names for solid colour shapes are intentionally not offered.  A
# ``blue_heart`` still belongs to the heart bucket because it is not a solid
# square/circle; only names containing one of these shape words are excluded.
SOLID_SHAPE_TERMS: Final[frozenset[str]] = frozenset(
    {
        "circle",
        "diamond",
        "hexagon",
        "square",
        "triangle",
    }
)
COLOUR_TERMS: Final[frozenset[str]] = frozenset(
    {
        "black",
        "blue",
        "brown",
        "gray",
        "green",
        "grey",
        "orange",
        "pink",
        "purple",
        "red",
        "white",
        "yellow",
    }
)

CUSTOM_RACING_EMOJI_RE: Final[re.Pattern[str]] = re.compile(
    r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_~]{1,32}):(?P<id>[0-9]{1,20})>$"
)


@dataclass(frozen=True, slots=True)
class CustomRacingEmoji:
    """Validated custom Discord emoji markup."""

    name: str
    emoji_id: int
    animated: bool = False

    @property
    def rendered(self) -> str:
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.emoji_id}>"


@dataclass(frozen=True, slots=True)
class RacingEmojiInfo:
    """Normalized classification used by shop and purchase callers."""

    value: str
    category: RacingEmojiCategory
    price: int
    name: str | None = None
    custom: CustomRacingEmoji | None = None


def parse_custom_racing_emoji(value: str) -> CustomRacingEmoji | None:
    """Parse a custom Discord emoji, returning ``None`` for Unicode input."""

    match = CUSTOM_RACING_EMOJI_RE.fullmatch(value.strip())
    if match is None:
        return None
    return CustomRacingEmoji(
        name=match.group("name"),
        emoji_id=int(match.group("id")),
        animated=bool(match.group("animated")),
    )


def _unicode_name(value: str) -> str:
    """Return the lower-case CLDR name for one normalized Unicode emoji."""

    # Discord users commonly paste an optional variation selector.  The emoji
    # package maps the selector-free form in some releases, so use it for the
    # lookup while retaining the original value for rendering/storage.
    normalized = value.replace("\ufe0e", "").replace("\ufe0f", "")
    data = emoji_lib.EMOJI_DATA.get(value) or emoji_lib.EMOJI_DATA.get(normalized)
    if data is None:
        return ""
    english = str(data.get("en") or "")
    return english.removeprefix(":").removesuffix(":").casefold()


def _is_one_unicode_emoji(value: str) -> bool:
    normalized = value.replace("\ufe0e", "").replace("\ufe0f", "")
    matches = emoji_lib.emoji_list(normalized)
    return (
        len(matches) == 1
        and int(matches[0]["match_start"]) == 0
        and int(matches[0]["match_end"]) == len(normalized)
    )


def classify_racing_emoji(value: str) -> RacingEmojiInfo:
    """Validate and classify one Unicode or custom Discord emoji.

    ``ValueError`` is raised for blank, malformed, multiple, or unsupported
    values.  Solid colour shape emoji are valid Discord emoji but deliberately
    rejected because they are reserved for the race board UI.
    """

    value = value.strip()
    if not value:
        raise ValueError("An emoji is required.")

    custom = parse_custom_racing_emoji(value)
    if custom is not None:
        return RacingEmojiInfo(
            value=custom.rendered,
            category="custom",
            price=RACING_EMOJI_PRICES["custom"],
            name=custom.name,
            custom=custom,
        )

    if not _is_one_unicode_emoji(value):
        raise ValueError("Provide one Unicode or custom Discord emoji.")

    name = _unicode_name(value)
    terms = frozenset(name.split("_"))
    if terms & SOLID_SHAPE_TERMS and terms & COLOUR_TERMS:
        raise ValueError("Solid colour square/circle emoji cannot be used for racing.")

    if terms & SEA_ANIMAL_TERMS or name in DEFAULT_RACE_SEA_ANIMALS:
        category: RacingEmojiCategory = "sea_animal"
    elif terms & ANIMAL_TERMS:
        category = "animal"
    elif "heart" in terms or "heart" in name:
        category = "heart"
    elif terms & FOOD_TERMS or any(term in name for term in FOOD_TERMS):
        category = "food"
    elif terms & HUMAN_FACE_TERMS or any(term in HUMAN_FACE_TERMS for term in terms):
        category = "human_face"
    else:
        category = "misc"
    return RacingEmojiInfo(
        value=value,
        category=category,
        price=RACING_EMOJI_PRICES[category],
        name=name or None,
    )


def racing_emoji_price(value: str) -> int:
    """Return the Coins price for one racing emoji."""

    return classify_racing_emoji(value).price


def racing_emoji_category(value: str) -> RacingEmojiCategory:
    """Return the price/category bucket for one racing emoji."""

    return classify_racing_emoji(value).category
