"""Small, dependency-free helpers for Mudae wish tracking.

The Mudae bot does not expose a public API for wishes.  These helpers keep the
matching rules in one place so the listener and command handlers use exactly
the same normalisation and embed parsing behaviour.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

# ``$imab`` renders one bundle over one or more pages.  Keep the parser and
# persistence-facing models here so command handlers and the event listener
# can share the same normalization rules without importing Discord-specific
# code into the data layer.
BUNDLE_MARKER_RE = re.compile(r"\s*\(\s*bundle\s*\)\s*$", re.IGNORECASE)
BUNDLE_ENTRY_RE = re.compile(
    r"^\s*(?:[·•\-]\s*)?(.+?)\s*\(\s*\*{0,2}([\d,]+)\*{0,2}\s*\)\s*$"
)
BUNDLE_PAGE_RE = re.compile(r"(?:^|\bpage\s+)\s*(\d+)\s*/\s*(\d+)\s*$", re.IGNORECASE)

# Mudae has emitted both ``**45**<:kakera:...>`` and
# ``**45<:kakera:...>**`` over time (and ``:kakera:`` appears in fixtures and
# forwarded messages).  Keep the emoji in the expression so unrelated bold
# numbers in an embed cannot be mistaken for a character's kakera value.
KAKERA_RE = re.compile(
    r"\*\*\s*([\d,]+)\s*(?:<a?:kakera:\d+>|:kakera:)\s*\*\*"
    r"|\*\*\s*([\d,]+)\s*\*\*\s*(?:<a?:kakera:\d+>|:kakera:)",
    re.IGNORECASE,
)
FOOTER_KAKERA_RE = re.compile(r"\s-\s*([\d,]+)\s+ka\b", re.IGNORECASE)
NON_SPAWN_MARKERS_RE = re.compile(
    r"(?:^|\n)\s*\*{0,2}\s*(?:claim\s+rank|like\s+rank|avg|"
    r"top\s+10\s+value|total\s+value|collection\s+size|"
    r"pok[ée]dex|reacts|keys)\s*:\s*\*{0,2}"
    r"|animanga\s+roulette",
    re.IGNORECASE,
)
PAGINATION_FOOTER_RE = re.compile(r"^\s*(?:page\s+)?\d+\s*/\s*\d+\s*$", re.IGNORECASE)
# Character cards that have already been claimed can still contain the same
# kakera/character data as a fresh roll.  Mudae identifies those cards in the
# footer (for example, ``Belongs to kelgorathh``), so they must not trigger a
# wish or kakera notification.
CLAIMED_FOOTER_RE = re.compile(
    r"\b(?:belongs\s+to|already\s+claimed|claimed(?:\s+by)?)\b",
    re.IGNORECASE,
)


def normalize_wish(value: str) -> str:
    """Return a stable, case-insensitive key for a wish value."""

    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.strip().casefold().split())


def split_series_values(value: str) -> tuple[str, ...]:
    """Split a ``wishseries`` argument into unique, display-ready values.

    Mudae series names are separated with ``$`` in Fishie's command syntax.
    Empty values (for example, a trailing separator) are ignored and repeated
    values are collapsed case-insensitively while preserving the first
    spelling supplied by the user.  The helper deliberately returns the
    un-normalized values so callers can show the original names in responses;
    use :func:`normalize_wish` when persisting or comparing them.
    """

    if not isinstance(value, str):
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for candidate in value.split("$"):
        text = " ".join(candidate.strip().split())
        if not text:
            continue
        key = normalize_wish(text)
        if key in seen:
            continue
        seen.add(key)
        values.append(text)
    return tuple(values)


# Keep an intuitive alias for command handlers that call the operation a
# "wish" rather than a generic value split.
split_series_wishes = split_series_values


def normalize_bundle_name(value: str) -> str:
    """Normalize the visible name of a Mudae ``(Bundle)`` embed.

    Mudae may put the marker on a new line or vary its casing.  Removing only
    the trailing marker leaves legitimate ``(Bundle)`` text in a name intact
    while making ``bundle_key`` stable across page edits.
    """

    if not isinstance(value, str):
        return ""
    value = value.replace("\\r", "").replace("\\n", "\n")
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    return BUNDLE_MARKER_RE.sub("", value).strip()


def bundle_key(value: str) -> str:
    """Return the case/whitespace-insensitive key for a bundle name."""

    return normalize_wish(normalize_bundle_name(value))


@dataclass(frozen=True, slots=True)
class MudaeSeriesBundleEntry:
    """A series listed on one page of a Mudae bundle response."""

    name: str
    normalized_name: str
    character_count: int | None = None
    page: int | None = None
    position: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedMudaeSeriesBundle:
    """The bundle and entries parsed from an ``$imab`` embed."""

    name: str
    key: str
    entries: tuple[MudaeSeriesBundleEntry, ...]
    page: int | None = None
    total_pages: int | None = None

    @property
    def bundle_name(self) -> str:
        """Compatibility alias used by persistence code."""

        return self.name

    @property
    def bundle_key(self) -> str:
        """Compatibility alias used by persistence code."""

        return self.key


def _embed_value(embed: object, key: str, default: Any = None) -> Any:
    """Read a key from a dict-like fixture or a ``discord.Embed`` object."""

    if isinstance(embed, Mapping):
        return embed.get(key, default)
    return getattr(embed, key, default)


def parse_mudae_series_bundle(embed: object) -> Optional[ParsedMudaeSeriesBundle]:
    """Parse a paginated ``$imab`` bundle embed.

    The function intentionally accepts dictionaries as well as Discord embed
    instances, which keeps gateway fixtures and migration tests dependency
    free.  It returns ``None`` for normal character rolls and unrelated
    information embeds.  Each entry includes its visible name, a normalized
    comparison key, and (when Mudae provided it) the character count.
    """

    author = _embed_value(embed, "author") or {}
    author_name = (
        author.get("name")
        if isinstance(author, Mapping)
        else getattr(author, "name", None)
    )
    if not isinstance(author_name, str) or not BUNDLE_MARKER_RE.search(author_name):
        return None

    name = normalize_bundle_name(author_name)
    key = bundle_key(name)
    if not name or not key:
        return None

    description = _embed_value(embed, "description", "") or ""
    if not isinstance(description, str):
        description = str(description)

    footer = _embed_value(embed, "footer") or {}
    footer_text = (
        footer.get("text")
        if isinstance(footer, Mapping)
        else getattr(footer, "text", None)
    )
    if not isinstance(footer_text, str):
        footer_text = ""
    page: int | None = None
    total_pages: int | None = None
    page_match = BUNDLE_PAGE_RE.search(footer_text.strip())
    if page_match:
        page = int(page_match.group(1))
        total_pages = int(page_match.group(2))

    entries: list[MudaeSeriesBundleEntry] = []
    for line in description.splitlines():
        match = BUNDLE_ENTRY_RE.match(line)
        if not match:
            continue
        entry_name = match.group(1).strip().strip("`*_ ")
        if not entry_name:
            continue
        # Do not treat a malformed count as an entry.  The regex only permits
        # digits and commas, but keeping this conversion guarded makes the
        # helper robust to future parser changes and hand-written fixtures.
        try:
            count = int(match.group(2).replace(",", ""))
        except ValueError:
            continue
        entries.append(
            MudaeSeriesBundleEntry(
                name=entry_name,
                normalized_name=normalize_wish(entry_name),
                character_count=count,
                page=page,
                position=len(entries) + 1,
            )
        )

    return ParsedMudaeSeriesBundle(
        name=name,
        key=key,
        entries=tuple(entries),
        page=page,
        total_pages=total_pages,
    )


# ``parse_series_bundle`` is shorter and useful to call sites that already
# know the message came from a series scrape.
parse_series_bundle = parse_mudae_series_bundle


@dataclass(frozen=True, slots=True)
class MudaeWishRecord:
    """Database representation of one user wish.

    All fields after ``wish_value`` are optional so rows produced by migration
    0060 (before metadata existed) can still be converted safely.  ``id`` is
    the stable identifier introduced by migration 0085.
    """

    guild_id: int
    user_id: int
    wish_type: str
    wish_value: str
    id: int | None = None
    normalized_value: str | None = None
    bundle_id: int | None = None
    bundle_key: str | None = None
    bundle_name: str | None = None
    bundle_created_at: datetime | None = None
    source_guild_id: int | None = None
    source_channel_id: int | None = None
    source_message_id: int | None = None
    source_page: int | None = None
    source_entry: int | None = None
    kakera_threshold: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def value_key(self) -> str:
        """Return the normalized value used for matching."""

        return self.normalized_value or normalize_wish(self.wish_value)

    @property
    def is_series(self) -> bool:
        return self.wish_type in {"series", "series_kakera"}

    @property
    def is_series_kakera(self) -> bool:
        return self.wish_type == "series_kakera" or (
            self.is_series and self.kakera_threshold is not None
        )


# Explicit aliases make the model discoverable to callers that prefer a
# series-specific name while keeping one canonical representation for all wish
# types.
MudaeSeriesWish = MudaeWishRecord
SeriesWishRecord = MudaeWishRecord


def _row_value(row: Mapping[str, Any] | object, key: str, default: Any = None) -> Any:
    """Read a value from an asyncpg Record or a regular mapping/object."""

    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def mudae_wish_from_row(row: Mapping[str, Any] | object) -> MudaeWishRecord:
    """Convert a database row into a metadata-aware :class:`MudaeWishRecord`."""

    wish_value = str(_row_value(row, "wish_value", ""))
    wish_type = str(_row_value(row, "wish_type", ""))
    guild_id = int(_row_value(row, "guild_id", 0))
    source_guild_id = _optional_int(_row_value(row, "source_guild_id"))
    return MudaeWishRecord(
        guild_id=guild_id,
        user_id=int(_row_value(row, "user_id", 0)),
        wish_type=wish_type,
        wish_value=wish_value,
        id=_optional_int(_row_value(row, "id")),
        normalized_value=(
            str(_row_value(row, "normalized_value"))
            if _row_value(row, "normalized_value") is not None
            else normalize_wish(wish_value)
        ),
        bundle_id=_optional_int(_row_value(row, "bundle_id")),
        bundle_key=(
            str(_row_value(row, "bundle_key"))
            if _row_value(row, "bundle_key") is not None
            else None
        ),
        bundle_name=(
            str(_row_value(row, "bundle_name"))
            if _row_value(row, "bundle_name") is not None
            else None
        ),
        bundle_created_at=_row_value(row, "bundle_created_at"),
        source_guild_id=(source_guild_id if source_guild_id is not None else guild_id),
        source_channel_id=_optional_int(_row_value(row, "source_channel_id")),
        source_message_id=_optional_int(_row_value(row, "source_message_id")),
        source_page=_optional_int(_row_value(row, "source_page")),
        source_entry=_optional_int(_row_value(row, "source_entry")),
        kakera_threshold=_optional_int(_row_value(row, "kakera_threshold")),
        created_at=_row_value(row, "created_at"),
        updated_at=_row_value(row, "updated_at"),
    )


def series_wish_from_row(row: Mapping[str, Any] | object) -> MudaeWishRecord | None:
    """Convert a row only when it represents a series wish."""

    record = mudae_wish_from_row(row)
    return record if record.is_series else None


def series_wish_values(
    series: str,
    *,
    wish_type: str = "series",
    kakera_threshold: int | None = None,
    bundle_id: int | None = None,
    bundle_name: str | None = None,
    source_guild_id: int | None = None,
    source_channel_id: int | None = None,
    source_message_id: int | None = None,
    source_page: int | None = None,
    source_entry: int | None = None,
) -> dict[str, Any]:
    """Build normalized insert values for a series wish row.

    The returned mapping intentionally omits guild/user ids because those are
    command-scope values.  It can be passed directly to an INSERT builder or
    used to validate a batch before opening a transaction.
    """

    text = " ".join(str(series).strip().split())
    if not text:
        raise ValueError("A series name is required")
    if wish_type not in {"series", "series_kakera"}:
        raise ValueError("wish_type must be series or series_kakera")
    if wish_type == "series_kakera" and kakera_threshold is None:
        raise ValueError("series_kakera wishes require a kakera threshold")
    if kakera_threshold is not None and kakera_threshold < 0:
        raise ValueError("kakera threshold cannot be negative")
    normalized = normalize_wish(text)
    key = bundle_key(bundle_name) if bundle_name else None
    return {
        "wish_type": wish_type,
        "wish_value": normalized,
        "normalized_value": normalized,
        "kakera_threshold": kakera_threshold,
        "bundle_id": bundle_id,
        "bundle_key": key,
        "bundle_name": bundle_name,
        "source_guild_id": source_guild_id,
        "source_channel_id": source_channel_id,
        "source_message_id": source_message_id,
        "source_page": source_page,
        "source_entry": source_entry,
    }


def wish_item_key(kind: str, value: str | int) -> str:
    """Build the in-memory key used for a character/series/kakera wish."""

    if kind not in {"character", "series", "series_kakera", "kakera"}:
        raise ValueError(f"Unsupported Mudae wish type: {kind}")
    if kind == "kakera":
        return f"kakera:{int(value)}"
    return f"{kind}:{normalize_wish(str(value))}"


def _extract_series(description: str, footer: str | None) -> str | None:
    """Extract a series from the two Mudae embed layouts in the wild."""

    lines = [line.strip() for line in description.splitlines() if line.strip()]
    if lines:
        series_lines: list[str] = []
        for line in lines:
            # The series occupies the leading description block. Stop before
            # kakera/reaction instructions so multiline titles are preserved
            # without including the rest of the card.
            if KAKERA_RE.search(line) or re.search(
                r"\bReact\s+with\b", line, re.IGNORECASE
            ):
                break
            if line.startswith("**"):
                break
            series_lines.append(line)
        if series_lines:
            # Keep the parsed value useful to callers as well as to the
            # normalized matcher by replacing line breaks with spaces.
            return " ".join(series_lines)

    if footer:
        # Footer format: ``Character / Series - 45 ka``.
        left = footer.split(" - ", 1)[0]
        if " / " in left:
            series = left.split(" / ", 1)[1].strip()
            if series:
                return series
    return None


@dataclass(frozen=True)
class ParsedMudaeWish:
    """The values that can be matched against a user's wishes."""

    character: str
    series: str | None
    kakera: int | None


def parse_mudae_embed(embed: object) -> Optional[ParsedMudaeWish]:
    """Parse a Mudae character roll embed.

    ``discord.Embed`` and the small dictionaries used by gateway fixtures are
    both accepted.  Returning ``None`` for an embed without a character name
    keeps unrelated Mudae messages from triggering notifications.
    """

    if isinstance(embed, dict):
        author = embed.get("author") or {}
        character = author.get("name") if isinstance(author, dict) else None
        description = embed.get("description") or ""
        footer = embed.get("footer") or {}
        footer_value = footer.get("text") if isinstance(footer, dict) else None
        footer_text = footer_value if isinstance(footer_value, str) else None
    else:
        author = getattr(embed, "author", None)
        character = getattr(author, "name", None)
        description = getattr(embed, "description", "") or ""
        footer = getattr(embed, "footer", None)
        footer_value = getattr(footer, "text", None) if footer else None
        footer_text = footer_value if isinstance(footer_value, str) else None

    if not isinstance(character, str) or not character.strip():
        return None

    # ``$im`` (and similar information/list commands) uses the same author
    # field as a roll, but its embed is not a spawn.  In particular it carries
    # claim/like rankings and a ``1 / 79`` pagination footer.  Reject those
    # layouts before extracting a character or series so wish notifications
    # are only sent for actual rolls.
    if NON_SPAWN_MARKERS_RE.search(description):
        return None
    if footer_text and PAGINATION_FOOTER_RE.fullmatch(footer_text):
        return None
    if footer_text and CLAIMED_FOOTER_RE.search(footer_text):
        return None

    match = KAKERA_RE.search(description)
    if match:
        kakera_text = match.group(1) or match.group(2)
        kakera = int(kakera_text.replace(",", ""))
    else:
        footer_match = FOOTER_KAKERA_RE.search(footer_text or "")
        kakera = int(footer_match.group(1).replace(",", "")) if footer_match else None

    # A character roll has a kakera value (in the description or the compact
    # footer layout).  Requiring it prevents unrelated Mudae embeds that happen
    # to expose an author name from triggering a wish notification.
    if kakera is None:
        return None

    series = _extract_series(description, footer_text)
    return ParsedMudaeWish(character=character.strip(), series=series, kakera=kakera)
