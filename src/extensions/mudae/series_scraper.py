"""Helpers for discovering series from Mudae ``$imab`` responses.

Mudae does not expose an API for its bundle/search commands.  A bundle is
rendered as a normal Discord embed and can span several pages; this module
contains the small, side-effect free parser used by commands and the optional
auto-scrape event.  Database writes deliberately stay outside this module so
callers can decide how to persist a series (and how to handle permissions or
privacy settings).

The parser accepts both :class:`discord.Embed` instances and the dictionaries
returned by raw-message payloads.  ``$imab`` embeds have historically changed
their whitespace and markdown slightly, therefore series names are matched by
their trailing character count rather than by a fixed line format.  Wrapped
series names and page footers are handled as well.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Final, cast

import discord

# Keep this local instead of importing ``sphere`` so the parser can be used by
# event modules without importing command registration code.
MUDAE_USER_ID: Final[int] = 432610292342587392
MudaeID: Final[int] = MUDAE_USER_ID

_BUNDLE_RE = re.compile(r"\bbundle\b", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[·•▪◦‣]|[-*])\s+")
_ENTRY_COUNT_RE = re.compile(r"\(\s*\*{0,3}\s*(?P<count>[\d,]+)\s*\*{0,3}\s*\)\s*$")
_PAGE_RE = re.compile(
    r"(?:\bpage\s*)?(?P<page>\d+)\s*(?:/|of)\s*(?P<pages>\d+)\s*$",
    re.IGNORECASE,
)


def normalize_series_name(value: str) -> str:
    """Return a case/whitespace-insensitive series key.

    Newline wrapping in Mudae embeds is presentation-only.  Unicode NFKC also
    makes full-width punctuation and compatibility characters compare as users
    expect when they type a series name for a wish.
    """

    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.replace("\u200b", "").split()).casefold()


# Alias matching the existing wish helper's naming convention.  Keeping both
# names makes this module straightforward to consume from old command code.
normalize_series = normalize_series_name


@dataclass(frozen=True, slots=True)
class MudaeSeriesEntry:
    """One series listed by a Mudae bundle."""

    name: str
    character_count: int | None = None

    @property
    def normalized_name(self) -> str:
        return normalize_series_name(self.name)

    @property
    def count(self) -> int | None:
        """Short alias used by callers rendering a bundle list."""

        return self.character_count


@dataclass(frozen=True, slots=True)
class MudaeSeriesBundle:
    """Parsed metadata and entries from one page of an ``$imab`` response."""

    name: str
    entries: tuple[MudaeSeriesEntry, ...] = ()
    page: int | None = None
    pages: int | None = None
    message_id: int | None = None
    guild_id: int | None = None
    channel_id: int | None = None

    @property
    def bundle_name(self) -> str:
        """Compatibility alias used by persistence code."""

        return self.name

    @property
    def bundle_key(self) -> str:
        return normalize_series_name(self.name)

    @property
    def total_pages(self) -> int | None:
        return self.pages

    @property
    def series(self) -> tuple[MudaeSeriesEntry, ...]:
        return self.entries


def _mapping_or_attr(value: object, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _embed_author_name(embed: object) -> str | None:
    author = _mapping_or_attr(embed, "author")
    name = _mapping_or_attr(author, "name")
    if not isinstance(name, str) or not name.strip():
        return None
    # Forwarded/raw fixtures can retain JSON escape sequences literally.
    name = name.replace("\\r", "").replace("\\n", "\n")
    return name.strip()


def _embed_description(embed: object) -> str:
    value = _mapping_or_attr(embed, "description", "")
    if not isinstance(value, str):
        return ""
    # Raw-message fixtures occasionally contain escaped line breaks.  Do not
    # alter normal backslashes in names unless no real line breaks are present.
    if "\\n" in value and "\n" not in value:
        value = value.replace("\\r", "").replace("\\n", "\n")
    return value


def _embed_footer_text(embed: object) -> str | None:
    footer = _mapping_or_attr(embed, "footer")
    value = _mapping_or_attr(footer, "text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bundle_name(author_name: str) -> str | None:
    """Extract the bundle name from Mudae's author title."""

    if not _BUNDLE_RE.search(author_name):
        return None
    # Most responses use ``Series name (Bundle)``.  Also accept bracketed or
    # colon-prefixed variants seen in forwarded/older Mudae messages.
    name = re.sub(r"\s*[\[(]\s*bundle\s*[\])]\s*$", "", author_name, flags=re.I)
    name = re.sub(r"\s*[-:|]\s*bundle\s*$", "", name, flags=re.I)
    name = re.sub(r"^\s*bundle\s*[-:|]\s*", "", name, flags=re.I)
    name = " ".join(name.split())
    return name or None


def _parse_page(footer: str | None) -> tuple[int | None, int | None]:
    if not footer:
        return None, None
    match = _PAGE_RE.search(footer)
    if match is None:
        return None, None
    page, pages = int(match.group("page")), int(match.group("pages"))
    if page < 1 or pages < 1 or page > pages:
        return None, None
    return page, pages


def _entry_from_text(value: str) -> MudaeSeriesEntry | None:
    match = _ENTRY_COUNT_RE.search(value)
    if match is None:
        return None
    name = value[: match.start()].strip()
    # Remove only markdown that wraps the whole title.  Asterisks inside a
    # legitimate title (rare, but possible) should not be discarded.
    name = re.sub(r"^\*+|\*+$", "", name).strip()
    if not name:
        return None
    return MudaeSeriesEntry(
        name=name, character_count=int(match.group("count").replace(",", ""))
    )


def _parse_entries(description: str) -> tuple[MudaeSeriesEntry, ...]:
    """Parse bullet entries, including entries wrapped across lines."""

    lines = [line.strip() for line in description.splitlines() if line.strip()]
    current: str | None = None
    entries: list[MudaeSeriesEntry] = []

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        entry = _entry_from_text(current)
        if entry is not None:
            entries.append(entry)
        current = None

    for line in lines:
        bullet = _BULLET_RE.match(line)
        if bullet:
            flush()
            current = line[bullet.end() :].strip()
            # A complete item can be flushed now, but retaining it until the
            # next bullet also handles a harmless continuation line robustly.
            if _ENTRY_COUNT_RE.search(current):
                flush()
            continue

        if current is None:
            # Leading ``381 chars``/``382 total chars`` statistics are not
            # entries.  If a Mudae version omits bullets, accepting a line with
            # a trailing count still gives a useful result.
            candidate = _entry_from_text(line)
            if candidate is not None:
                entries.append(candidate)
            continue

        current = f"{current} {line}".strip()
        if _ENTRY_COUNT_RE.search(current):
            flush()

    flush()

    # Mudae can repeat an entry when a page is edited.  Preserve order but
    # avoid duplicate normalized names in the parsed page.
    seen: set[str] = set()
    unique: list[MudaeSeriesEntry] = []
    for entry in entries:
        key = entry.normalized_name
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return tuple(unique)


def parse_series_bundle_embed(embed: object) -> MudaeSeriesBundle | None:
    """Parse a Mudae ``$imab`` bundle embed, or return ``None``.

    The author title must contain ``Bundle``.  This guard is important because
    ordinary ``$im`` character cards use a similar author field but should not
    be interpreted as an auto-scrape result.
    """

    author_name = _embed_author_name(embed)
    if author_name is None:
        return None
    name = _bundle_name(author_name)
    if name is None:
        return None
    page, pages = _parse_page(_embed_footer_text(embed))
    return MudaeSeriesBundle(
        name=name,
        entries=_parse_entries(_embed_description(embed)),
        page=page,
        pages=pages,
    )


# A descriptive alias for callers that already refer to parser functions by
# their Mudae-specific name.
parse_mudae_series_bundle = parse_series_bundle_embed
parse_mudae_bundle_embed = parse_series_bundle_embed
parse_imab_bundle = parse_series_bundle_embed
parse_imab_embed = parse_series_bundle_embed


def _object_id(value: object, key: str = "id") -> int | None:
    raw = _mapping_or_attr(value, key)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def is_mudae_message(message: object) -> bool:
    """Return whether a message was authored by Mudae.

    Raw payloads may expose ``application_id``/``webhook_id`` while gateway
    objects expose ``message.author.id``.  Accepting all three makes this
    helper useful for forwarded-message snapshots too.
    """

    author = _mapping_or_attr(message, "author")
    author_id = _object_id(author)
    if author_id == MUDAE_USER_ID:
        return True
    return any(
        _object_id(message, key) == MUDAE_USER_ID
        for key in ("application_id", "webhook_id")
    )


def parse_mudae_bundle_message(message: object) -> MudaeSeriesBundle | None:
    """Parse a message and attach source IDs when it is a Mudae bundle."""

    if not is_mudae_message(message):
        return None
    embeds = _mapping_or_attr(message, "embeds", ()) or ()
    for embed in embeds:
        parsed = parse_series_bundle_embed(embed)
        if parsed is None:
            continue
        guild_id = _object_id(_mapping_or_attr(message, "guild"))
        # Raw payloads usually provide guild/channel IDs directly.  Gateway
        # messages may only have ``guild.id`` and ``channel.id``.
        guild_id = _object_id(message, "guild_id") or guild_id
        channel_id = _object_id(_mapping_or_attr(message, "channel"))
        channel_id = _object_id(message, "channel_id") or channel_id
        return replace(
            parsed,
            message_id=_object_id(message),
            guild_id=guild_id,
            channel_id=channel_id,
        )
    return None


parse_mudae_bundle = parse_mudae_bundle_message
is_mudae_bundle_message = is_mudae_message


async def _fetch_reference_message(
    channel: object, reference: object | None
) -> object | None:
    if reference is None:
        return None
    resolved = _mapping_or_attr(reference, "resolved")
    if resolved is not None and is_mudae_message(resolved):
        return resolved
    message_id = _object_id(reference, "message_id") or _object_id(reference)
    fetch_message = getattr(channel, "fetch_message", None)
    if message_id is None or not callable(fetch_message):
        return None
    try:
        candidate = await cast(Any, fetch_message)(message_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return None
    return candidate if is_mudae_message(candidate) else None


async def find_mudae_bundle_message(
    channel: discord.abc.Messageable,
    *,
    reference: object | None = None,
    limit: int = 10,
) -> discord.Message | None:
    """Find a Mudae bundle by reply first, then in recent channel history.

    The search is intentionally capped at ten messages by default.  It avoids
    broad history scans while still handling the common case where a user runs
    ``scrapeseries`` immediately after ``$imab``.
    """

    if limit < 1:
        return None
    referenced = await _fetch_reference_message(channel, reference)
    if referenced is not None and parse_mudae_bundle_message(referenced) is not None:
        return referenced  # type: ignore[return-value]

    history = getattr(channel, "history", None)
    if not callable(history):
        return None
    try:
        messages = cast(Any, history)(limit=min(limit, 10))
        async for candidate in messages:
            if parse_mudae_bundle_message(candidate) is not None:
                return candidate
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return None
    return None


async def find_mudae_bundle_for_context(
    ctx: object, *, limit: int = 10
) -> discord.Message | None:
    """Convenience wrapper for command contexts.

    ``ctx.message.reference`` is used when available; otherwise the latest ten
    messages in ``ctx.channel`` are searched.
    """

    channel = _mapping_or_attr(ctx, "channel")
    if channel is None:
        return None
    command_message = _mapping_or_attr(ctx, "message")
    reference = _mapping_or_attr(command_message, "reference")
    return await find_mudae_bundle_message(channel, reference=reference, limit=limit)


async def scrape_series_from_context(
    ctx: object, *, limit: int = 10
) -> MudaeSeriesBundle | None:
    """Find and parse a bundle for a ``scrapeseries`` command.

    A reply is preferred, then the latest ten messages in the command channel
    are checked.  The returned bundle carries its source message, guild, and
    channel IDs so command code can persist those fields without another
    lookup.  This function performs no database writes.
    """

    message = await find_mudae_bundle_for_context(ctx, limit=limit)
    return parse_mudae_bundle_message(message) if message is not None else None


scrape_mudae_series = scrape_series_from_context


async def iter_recent_mudae_bundles(
    channel: discord.abc.Messageable, *, limit: int = 10
) -> AsyncIterator[tuple[discord.Message, MudaeSeriesBundle]]:
    """Yield parsed bundles from recent history, newest first."""

    history = getattr(channel, "history", None)
    if not callable(history):
        return
    try:
        messages = cast(Any, history)(limit=min(max(limit, 1), 10))
        async for message in messages:
            parsed = parse_mudae_bundle_message(message)
            if parsed is not None:
                yield message, parsed
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return


def merge_bundle_pages(
    bundles: Iterable[MudaeSeriesBundle],
) -> tuple[MudaeSeriesEntry, ...]:
    """Merge entries from page changes while preserving first-seen order."""

    seen: set[str] = set()
    merged: list[MudaeSeriesEntry] = []
    for bundle in bundles:
        for entry in bundle.entries:
            key = entry.normalized_name
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return tuple(merged)


@dataclass(slots=True)
class MudaeSeriesAutoScraper:
    """Small in-memory auto-scrape coordinator for event listeners.

    The class deliberately does not know about the database.  Enable guilds
    after loading their ``guild_settings.mudae_auto_scrape_series`` values,
    then pass each message/edit to :meth:`handle_message`; callers can persist
    the returned bundle using their own transaction and permissions policy.
    ``bundles`` contains the merged pages observed during this process lifetime
    and is useful for avoiding duplicate inserts.
    """

    enabled_guilds: set[int] = field(default_factory=set)
    bundles: dict[tuple[int, str], MudaeSeriesBundle] = field(default_factory=dict)

    def set_enabled(self, guild_id: int, enabled: bool = True) -> None:
        if enabled:
            self.enabled_guilds.add(int(guild_id))
        else:
            self.enabled_guilds.discard(int(guild_id))

    def is_enabled(self, guild_id: int | None) -> bool:
        return guild_id is not None and int(guild_id) in self.enabled_guilds

    def clear_guild(self, guild_id: int) -> None:
        guild_id = int(guild_id)
        self.enabled_guilds.discard(guild_id)
        for key in tuple(self.bundles):
            if key[0] == guild_id:
                self.bundles.pop(key, None)

    def ingest(self, bundle: MudaeSeriesBundle) -> MudaeSeriesBundle:
        """Merge a page into the in-memory bundle cache and return the result."""

        guild_id = int(bundle.guild_id or 0)
        key = (guild_id, bundle.bundle_key)
        previous = self.bundles.get(key)
        if previous is None:
            self.bundles[key] = bundle
            return bundle
        merged = replace(
            bundle,
            entries=merge_bundle_pages((previous, bundle)),
            page=bundle.page,
            pages=bundle.pages or previous.pages,
            message_id=bundle.message_id or previous.message_id,
            channel_id=bundle.channel_id or previous.channel_id,
            guild_id=bundle.guild_id or previous.guild_id,
        )
        self.bundles[key] = merged
        return merged

    def handle_message(self, message: object) -> MudaeSeriesBundle | None:
        """Parse and ingest an event message when auto-scrape is enabled."""

        bundle = parse_mudae_bundle_message(message)
        if bundle is None or not self.is_enabled(bundle.guild_id):
            return None
        return self.ingest(bundle)
