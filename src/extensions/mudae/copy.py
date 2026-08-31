"""Validation and row preparation for copying Mudae wishes.

The commands which expose these helpers live in :mod:`extensions.mudae`.  This
module deliberately has no Discord or database dependencies: command handlers
can resolve a source guild, fetch rows, and then pass the rows through
``prepare_wish_copy`` before inserting them into the destination guild.  Keeping
the filtering here prevents the four copy variants from slowly acquiring
different privacy and normalisation rules.

Rows returned by asyncpg (and dictionaries used in tests) are accepted.  A
source row's primary key or destination identifiers are never copied; callers
receive a small, safe record containing only wish data and source metadata.
The optional metadata fields are retained so a newer schema can record where a
wish was copied from without making this helper depend on that schema.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from .wishes import normalize_wish

# PostgreSQL BIGINT (used by Discord snowflake columns) is signed.  Checking
# the upper bound here avoids surprising overflow errors much later in a
# command's INSERT statement.
MAX_SNOWFLAKE = (1 << 63) - 1
MIN_WISHKAKERA = 67

WishCopyMode: TypeAlias = Literal[
    "wishlist", "serieswishlist", "serieswishlistka", "all"
]

COPY_MODE_ALIASES: dict[str, WishCopyMode] = {
    "wishlist": "wishlist",
    "characters": "wishlist",
    "character": "wishlist",
    "serieswishlist": "serieswishlist",
    "serieswishes": "serieswishlist",
    "series": "serieswishlist",
    "serieswishlistka": "serieswishlistka",
    "serieswishlistkakera": "serieswishlistka",
    "serieska": "serieswishlistka",
    "all": "all",
    "everything": "all",
}

# ``series_kakera`` is the canonical type used by the new schema.  The
# ``series`` + ``kakera_threshold`` form is accepted as a legacy row below.
SUPPORTED_WISH_TYPES = frozenset({"character", "series", "series_kakera", "kakera"})
COPY_MODE_TYPES: dict[WishCopyMode, frozenset[str]] = {
    "wishlist": frozenset({"character"}),
    "serieswishlist": frozenset({"series"}),
    "serieswishlistka": frozenset({"series_kakera"}),
    "all": SUPPORTED_WISH_TYPES,
}


class WishCopyError(ValueError):
    """Base class for expected copy/validation errors."""


class InvalidWishCopyMode(WishCopyError):
    """Raised when a copy subcommand is not recognised."""


class InvalidSourceGuild(WishCopyError):
    """Raised when a source server ID is malformed or unavailable."""


class SameWishCopyGuild(WishCopyError):
    """Raised when source and destination servers are identical."""


def _row_value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    """Read a mapping or asyncpg Record without importing asyncpg."""

    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def parse_copy_mode(value: object) -> WishCopyMode:
    """Return a canonical copy mode for a command argument.

    Separators are ignored to make aliases such as ``series-wishlist-ka`` and
    ``serieswishlistka`` behave identically.  Unknown values raise a typed
    ``WishCopyError`` so command handlers can turn it into a short user-facing
    ``BadArgument`` response.
    """

    if not isinstance(value, str):
        raise InvalidWishCopyMode("The copy category must be a text value.")
    key = "".join(
        character for character in value.casefold().strip() if character.isalnum()
    )
    mode = COPY_MODE_ALIASES.get(key)
    if mode is None:
        choices = ", ".join(("wishlist", "serieswishlist", "serieswishlistka", "all"))
        raise InvalidWishCopyMode(f"Choose one of: {choices}.")
    return mode


def copy_types(mode: WishCopyMode | str) -> frozenset[str]:
    """Return the exact wish types selected by ``mode``."""

    canonical = parse_copy_mode(mode)
    return COPY_MODE_TYPES[canonical]


def parse_source_guild_id(value: object) -> int:
    """Parse a Discord server ID accepted by a copy command.

    A bool is rejected explicitly because ``bool`` is an ``int`` subclass.
    Leading/trailing whitespace is harmless, while mentions and names are not
    silently interpreted as IDs (the commands document ``<serverid>``).
    """

    if isinstance(value, bool):
        raise InvalidSourceGuild("Provide a server ID, not a boolean value.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.isdecimal():
            raise InvalidSourceGuild("Provide a valid source server ID.")
        result = int(text)
    else:
        raise InvalidSourceGuild("Provide a valid source server ID.")
    if result <= 0 or result > MAX_SNOWFLAKE:
        raise InvalidSourceGuild("Provide a valid source server ID.")
    return result


def validate_copy_scope(
    source_guild: object,
    destination_guild: object,
    *,
    available_guild_ids: Iterable[object] | None = None,
) -> tuple[int, int]:
    """Validate source/destination IDs and (optionally) bot visibility.

    ``available_guild_ids`` should be supplied from ``bot.guilds`` when a
    command is run.  Refusing an unknown source before querying rows avoids
    leaking whether a user has wishes in a server the bot cannot access.
    """

    source_id = parse_source_guild_id(source_guild)
    destination_id = parse_source_guild_id(destination_guild)
    if source_id == destination_id:
        raise SameWishCopyGuild("The source and destination servers are the same.")
    if available_guild_ids is not None:
        visible = {parse_source_guild_id(value) for value in available_guild_ids}
        if source_id not in visible:
            raise InvalidSourceGuild("I cannot access that source server.")
    return source_id, destination_id


def _normalise_type(row: Mapping[str, Any] | Any) -> tuple[str, int | None] | None:
    """Return a canonical wish type and optional kakera threshold.

    Older migrations may have stored a series-kakera wish as ``wish_type =
    'series'`` with a separate ``kakera_threshold`` value.  Treat that form as
    ``series_kakera`` so copying it does not silently turn a threshold wish into
    a normal series wish.
    """

    raw_type = _row_value(row, "wish_type")
    if not isinstance(raw_type, str):
        return None
    wish_type = raw_type.strip().casefold()
    threshold_value = _row_value(row, "kakera_threshold")
    threshold: int | None = None
    if threshold_value is not None and threshold_value != "":
        try:
            threshold = int(threshold_value)
        except (TypeError, ValueError):
            return None
        if threshold < MIN_WISHKAKERA:
            return None
    if wish_type == "series" and threshold is not None:
        wish_type = "series_kakera"
    if wish_type not in SUPPORTED_WISH_TYPES:
        return None
    if wish_type == "series_kakera" and threshold is None:
        # The 0085 schema requires a threshold for this type.  Skipping an
        # incomplete legacy row is safer than creating a row that can never be
        # inserted or that would notify for every kakera value.
        return None
    return wish_type, threshold


def _normalise_value(wish_type: str, value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if wish_type == "kakera":
        try:
            amount = int(text.replace(",", ""))
        except ValueError:
            return None
        if amount < MIN_WISHKAKERA:
            return None
        return str(amount)
    normalised = normalize_wish(text)
    return normalised or None


@dataclass(frozen=True, slots=True)
class WishCopyRow:
    """A validated wish ready to insert in a destination server.

    ``source_guild_id`` and ``source_created_at`` are metadata only.  A caller
    should use ``insert_values`` for the destination's identity fields and may
    persist the source fields when those columns exist in its migration.
    """

    wish_type: str
    wish_value: str
    kakera_threshold: int | None = None
    source_guild_id: int | None = None
    source_channel_id: int | None = None
    source_message_id: int | None = None
    source_page: int | None = None
    source_entry: int | None = None
    bundle_id: str | None = None
    bundle_key: str | None = None
    bundle_name: str | None = None
    source_created_at: Any = None
    source_bundle_created_at: Any = None

    @property
    def key(self) -> tuple[str, str, int | None]:
        """Stable identity used to remove duplicate source rows."""

        return self.wish_type, self.wish_value, self.kakera_threshold

    def insert_values(
        self,
        *,
        destination_guild_id: int,
        user_id: int,
    ) -> dict[str, Any]:
        """Return safe destination fields for an INSERT statement.

        The returned dictionary intentionally omits source IDs and any primary
        key from the source row.  Optional metadata can be added by a caller
        from the explicit ``source_*`` attributes.
        """

        guild_id = parse_source_guild_id(destination_guild_id)
        owner_id = parse_source_guild_id(user_id)
        values: dict[str, Any] = {
            "guild_id": guild_id,
            "user_id": owner_id,
            "wish_type": self.wish_type,
            "wish_value": self.wish_value,
        }
        if self.wish_type == "series_kakera":
            values["kakera_threshold"] = self.kakera_threshold
        return values


def prepare_wish_copy(
    rows: Iterable[Mapping[str, Any] | Any],
    *,
    mode: WishCopyMode | str,
    source_guild_id: int | None = None,
    source_user_id: int | None = None,
) -> tuple[WishCopyRow, ...]:
    """Validate, select, normalise, and deduplicate rows for copying.

    Rows belonging to another guild/user are ignored when the corresponding
    expected ID is supplied.  This makes the helper safe to use with broad
    queries and protects against accidentally copying another member's data.
    Malformed rows (unknown types, empty values, invalid kakera thresholds) are
    skipped rather than making the whole copy fail.  The caller can report the
    returned count and leave malformed legacy rows untouched.
    """

    canonical = parse_copy_mode(mode)
    selected_types = COPY_MODE_TYPES[canonical]
    expected_guild = (
        parse_source_guild_id(source_guild_id) if source_guild_id is not None else None
    )
    expected_user = (
        parse_source_guild_id(source_user_id) if source_user_id is not None else None
    )

    prepared: list[WishCopyRow] = []
    seen: set[tuple[str, str, int | None]] = set()
    for row in rows:
        row_guild = _row_value(row, "guild_id")
        if expected_guild is not None and row_guild is not None:
            try:
                if parse_source_guild_id(row_guild) != expected_guild:
                    continue
            except InvalidSourceGuild:
                continue
        row_user = _row_value(row, "user_id")
        if expected_user is not None and row_user is not None:
            try:
                if parse_source_guild_id(row_user) != expected_user:
                    continue
            except InvalidSourceGuild:
                continue

        normalised_type = _normalise_type(row)
        if normalised_type is None:
            continue
        wish_type, threshold = normalised_type
        if wish_type not in selected_types:
            continue
        value = _normalise_value(wish_type, _row_value(row, "wish_value"))
        if value is None:
            continue
        key = (wish_type, value, threshold)
        if key in seen:
            continue
        seen.add(key)
        source_id = expected_guild
        if source_id is None and row_guild is not None:
            try:
                source_id = parse_source_guild_id(row_guild)
            except InvalidSourceGuild:
                source_id = None

        # IDs and timestamps are retained as explicit source metadata rather
        # than being blindly copied into the destination's primary-key or
        # creation columns.  This gives newer migrations enough information to
        # preserve attribution while keeping this helper compatible with the
        # original four-column table.
        def _optional_id(key: str) -> int | None:
            value = _row_value(row, key)
            if value is None or value == "":
                return None
            try:
                parsed = parse_source_guild_id(value)
            except InvalidSourceGuild:
                return None
            return parsed

        def _optional_position(key: str) -> int | None:
            value = _row_value(row, key)
            if value is None or value == "":
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        prepared.append(
            WishCopyRow(
                wish_type=wish_type,
                wish_value=value,
                kakera_threshold=threshold,
                source_guild_id=source_id,
                source_channel_id=_optional_id("source_channel_id"),
                source_message_id=_optional_id("source_message_id"),
                source_page=_optional_position("source_page"),
                source_entry=_optional_position("source_entry"),
                bundle_id=(
                    str(_row_value(row, "bundle_id")).strip()
                    if _row_value(row, "bundle_id") not in (None, "")
                    else None
                ),
                bundle_key=(
                    normalize_wish(str(_row_value(row, "bundle_key")))
                    if _row_value(row, "bundle_key") not in (None, "")
                    else None
                ),
                bundle_name=(
                    str(_row_value(row, "bundle_name")).strip()
                    if _row_value(row, "bundle_name") not in (None, "")
                    else None
                ),
                source_created_at=_row_value(row, "created_at"),
                source_bundle_created_at=_row_value(
                    row, "bundle_created_at", _row_value(row, "bundle_created_at_at")
                ),
            )
        )
    return tuple(prepared)


# ``copy_wish_rows`` reads naturally in command handlers and is kept as a
# stable alias for callers that prefer the operation-oriented name.
copy_wish_rows = prepare_wish_copy


__all__ = [
    "COPY_MODE_ALIASES",
    "COPY_MODE_TYPES",
    "InvalidSourceGuild",
    "InvalidWishCopyMode",
    "MIN_WISHKAKERA",
    "SameWishCopyGuild",
    "SUPPORTED_WISH_TYPES",
    "WishCopyError",
    "WishCopyMode",
    "WishCopyRow",
    "copy_types",
    "copy_wish_rows",
    "parse_copy_mode",
    "parse_source_guild_id",
    "prepare_wish_copy",
    "validate_copy_scope",
]
